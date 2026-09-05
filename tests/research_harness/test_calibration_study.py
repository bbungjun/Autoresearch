"""#119 보정 표본·Brier 분해·동일 순위·채택 조건과 실행 경계를 검증한다."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from autoresearch.research_harness import recall_experiment
from tests.research_harness.test_recall_analysis import inputs as metric_inputs, observations
from tests.research_harness.test_recall_experiment import fake_models as fake_models, inputs as inputs
from tools import calibration_study as m
from tools import run_calibration_study as runner


def grid() -> list:
    rows = []
    for row in observations():
        if row["arm"] not in ("baseline15", "preference"):
            continue
        row["metrics"]["brier"] = .5 if row["arm"] == "baseline15" else .49
        rows.append(row)
        expanded = deepcopy(row)
        expanded["arm"] = "baseline_expanded" if row["arm"] == "baseline15" else "preference_expanded"
        expanded["metrics"]["brier"] -= .01
        rows.append(expanded)
    return rows


def test_expansion_uses_only_nonfit_rows_and_never_refits_model(tmp_path: Path, inputs: tuple,
                                                              fake_models: list) -> None:
    labels, features = inputs
    old = tmp_path / "model"
    recall_experiment.train_model(labels, features, old, 401, "preference", {}, lambda _: None)
    calls = []
    result = m.expanded_calibration(old, runner.digest(old / "receipt.json"), labels, features, tmp_path / "expanded", calls.append)
    assert len(fake_models) == 1 and calls == ["calibration"]
    assert result["expanded_rows"] == 2*result["original_rows"]
    assert result["expanded_positive"] == 2*result["original_positive"]
    data = pq.read_table(tmp_path / "expanded/input.parquet")
    split = recall_experiment.group_split(labels, 401)
    assert not set(data["user_id"].to_pylist()) & set(labels["user_id"].take(split["train"]).to_pylist())
    with pytest.raises(FileExistsError):
        m.expanded_calibration(old, runner.digest(old / "receipt.json"), labels, features, tmp_path / "expanded", calls.append)
    assert len(calls) == 1


def test_expansion_rejects_changed_training_input(tmp_path: Path, inputs: tuple, fake_models: list) -> None:
    labels, features = inputs
    old = tmp_path / "model"
    recall_experiment.train_model(labels, features, old, 401, "baseline15", {}, lambda _: None)
    with pytest.raises(ValueError, match="training_input_changed"):
        m.expanded_calibration(old, runner.digest(old / "receipt.json"), labels.take(list(reversed(range(len(labels))))),
                               features, tmp_path / "expanded", lambda _: pytest.fail("must not fit"))


def test_brier_decomposition_and_probability_bin_boundaries() -> None:
    y = np.array([0, 1, 0, 1, 0, 1, 0, 1])
    p = np.array([0, .01, .025, .05, .1, .2, .5, 1])
    result = m.probability_diagnostics(y, p)
    assert sum(b["rows"] for b in result["bins"]) == len(y)
    assert sum(b["brier_contribution"] for b in result["bins"]) == pytest.approx(result["brier"])
    assert sum(c["contribution"] for c in result["classes"].values()) == pytest.approx(np.mean((p-y)**2))


@pytest.mark.parametrize("p", [[np.nan, .5], [-.1, .5], [.5, 1.1], [.5]])
def test_diagnostics_reject_invalid_probabilities(p: list) -> None:
    with pytest.raises(ValueError):
        m.probability_diagnostics(np.array([0, 1]), np.array(p))


def test_supported_requires_both_calibration_and_adoption() -> None:
    rows = grid()
    assert m.study_summary(rows)["verdict"] == "supported"
    for r in rows:
        if r["arm"] == "preference_expanded":
            r["metrics"]["brier"] = .51
    assert m.study_summary(rows)["verdict"] == "not_supported"
    assert not m.study_summary(rows)["calibration_effect"]["preference"]["passed"]


@pytest.mark.parametrize("case", ["missing", "duplicate", "identity", "coverage", "ranking", "nonfinite"])
def test_grid_pair_and_validity_guards(case: str) -> None:
    rows = grid()
    if case == "missing":
        rows.pop()
    elif case == "duplicate":
        rows.append(rows[0])
    elif case == "identity":
        rows[0]["row_keys_sha256"] = 'f'*64
    elif case == "coverage":
        rows[0]["metrics"]["coverage"]["positive_rows"] += 1
    elif case == "ranking":
        rows[0]["metrics"]["ndcg_at_10"] += .01
    else:
        rows[0]["metrics"]["brier"] = float('nan')
        assert m.study_summary(rows)["verdict"] == "uninformative"
        return
    with pytest.raises(ValueError):
        m.study_summary(rows)


def test_fit_budget_and_no_other_kind(tmp_path: Path) -> None:
    study = runner.Study(argparse.Namespace(output=tmp_path), tmp_path, 'a'*40)
    with pytest.raises(ValueError):
        study.before_fit("model")
    for _ in range(12):
        study.before_fit("calibration")
    with pytest.raises(ValueError):
        study.before_fit("calibration")
    assert len(list((tmp_path / "fit-attempts").glob('*.json'))) == 12


def test_old_cohort_user_exclusion_requires_manifest_pin(tmp_path: Path) -> None:
    path = tmp_path/'world-10901/judge-owned/raw/manifest.json'
    runner.write_json(path, {'validation_users':['a'], 'reserved_final_users':['b']})
    world = {'cohort_seed':10901, 'raw_manifest_sha256':runner.digest(path)}
    assert runner.old_cohort_users(tmp_path,world) == {'a','b'}
    path.write_text('{"validation_users":[],"reserved_final_users":[]}')
    with pytest.raises(ValueError,match='manifest_pin_mismatch'):
        runner.old_cohort_users(tmp_path,world)


def test_predict_seals_eight_before_score_and_raw_is_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys, labels, raw, probability = metric_inputs()
    study = runner.Study(argparse.Namespace(output=tmp_path, previous_run=tmp_path/'previous'), tmp_path, 'a'*40)
    runner.write_json(tmp_path/'calibrations-sealed.json', {})
    for seed in m.SEEDS:
        for family in m.FAMILIES:
            study.models[f'10701/{seed}/{family}'] = 'a'*64
            study.calibrations[f'10701/{seed}/{family}'] = {"expanded": {"slope": .1,"intercept": -3}}
    monkeypatch.setattr(runner, 'predict_model', lambda *a, **k: (raw, probability))
    paths = study.predict(10701,'test',keys,pa.table({}))
    assert len(paths) == 8 and not list(tmp_path.rglob('metrics.json'))
    rows = study.score(10701,keys,labels,paths)
    assert len(rows) == 8
    for seed in m.SEEDS:
        values = [r['metrics']['recall_at_10'] for r in rows if r['seed']==seed]
        assert values == [1.]*4
    (paths[0][2]/'prediction.parquet').write_bytes(b'changed')
    with pytest.raises(ValueError, match='hash_changed'):
        study.score(10701,keys,labels,paths)


@pytest.mark.parametrize('valid', [True, False])
def test_phase_order_and_duplicate_claim_no_fit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bool) -> None:
    args = argparse.Namespace(**{n: tmp_path/n for n in ('output','previous_run','old_evaluation','model_dir','cache_dir')})
    monkeypatch.setattr(runner, 'code_hashes', lambda _: {})
    def git(cmd: list, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout='' if cmd[1]=='status' else str(tmp_path/'common') if '--git-common-dir' in cmd else 'a'*40)
    monkeypatch.setattr(runner.subprocess,'run',git)
    events = []
    for name in ('prepare','fit','fresh'):
        monkeypatch.setattr(runner.Study,name,lambda self,n=name: events.append(n))
    def develop(self: runner.Study) -> dict:
        events.append('develop')
        self.development = {'coverage_valid':valid,'verdict':'not_supported'}
        return self.development
    def final(self: runner.Study) -> dict:
        events.append('final')
        self.claims = 3
        runner.write_json(self.output/'final.json', {'verdict':'not_supported'})
        return {}
    monkeypatch.setattr(runner.Study,'develop',develop)
    monkeypatch.setattr(runner.Study,'final',final)
    monkeypatch.setattr(runner.Study,'verify_seal',lambda _: None)
    runner.run(args)
    assert events == ['prepare','fit','develop'] + (['fresh','final'] if valid else [])
    result = json.loads((args.output/'result.json').read_bytes())
    assert result['completed'] is valid
    args.output = tmp_path/'different-output'
    events.clear()
    with pytest.raises(FileExistsError):
        runner.run(args)
    assert events == ['prepare']
    failure = json.loads((args.output/'failure.json').read_bytes())
    assert failure['fit_attempts']==0 and 'fit' in failure['costs']


def test_cli_supervisor_timeout_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import create_autospec
    supervisor = create_autospec(runner.supervise, return_value=0)
    monkeypatch.setattr(runner, 'supervise', supervisor)
    args = ['calibration-study']
    for name in ('output', 'previous-run', 'old-evaluation', 'model-dir', 'cache-dir'):
        args.extend([f'--{name}', str(tmp_path/name)])
    monkeypatch.setattr(runner.sys, 'argv', args)
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 0
    supervisor.assert_called_once()
    assert supervisor.call_args.kwargs == {'timeout': runner.MAX_SECONDS}
    assert supervisor.call_args.args[0][-1] == '--worker'
