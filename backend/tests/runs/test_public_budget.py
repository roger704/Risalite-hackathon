from concurrent.futures import ThreadPoolExecutor

from app.application.runs.budget import PublicRunBudget


def test_budget_survives_restart_and_does_not_charge_same_run_twice(tmp_path):
    path = tmp_path / "budget.sqlite"
    assert PublicRunBudget(path, 1).admit("first")
    restarted = PublicRunBudget(path, 1)
    assert restarted.admit("first")
    assert not restarted.admit("second")


def test_competing_admissions_cannot_exceed_budget(tmp_path):
    budget = PublicRunBudget(tmp_path / "budget.sqlite", 3)
    with ThreadPoolExecutor(max_workers=8) as workers:
        admitted = list(workers.map(lambda i: budget.admit(str(i)), range(16)))
    assert sum(admitted) == 3


def test_zero_budget_disables_paid_runs(tmp_path):
    assert not PublicRunBudget(tmp_path / "budget.sqlite", 0).admit("first")
