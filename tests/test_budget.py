"""Budget manager tests."""
import pytest
import asyncio
from cato.budget import BudgetManager, BudgetExceeded


@pytest.fixture
def budget(tmp_path):
    return BudgetManager(session_cap=1.0, monthly_cap=10.0, budget_path=tmp_path / "budget.json")


@pytest.mark.asyncio
async def test_budget_fires_before_call(budget):
    # Exhaust the session budget
    await budget.check_and_deduct("claude-sonnet-4-6", 100000, 50000)
    # Next call should raise BudgetExceeded
    with pytest.raises(BudgetExceeded):
        await budget.check_and_deduct("claude-sonnet-4-6", 100000, 50000)


@pytest.mark.asyncio
async def test_budget_format_footer(budget):
    footer = budget.format_footer()
    assert "$" in footer


def test_unknown_model_raises(budget):
    with pytest.raises(ValueError, match="Unknown model"):
        asyncio.run(budget.check_and_deduct("unknown-model-xyz", 100, 50))
