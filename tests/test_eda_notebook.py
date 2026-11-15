"""Check EDA temporal alignment, coverage and metrics without full data or Spark."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(scope='module')
def helpers():
    """Load only the notebook helper definitions; never execute data-loading cells."""
    path = Path(__file__).resolve().parents[1] / 'notebooks/02_eda_h1.ipynb'
    notebook = json.loads(path.read_text(encoding='utf-8'))
    namespace = {'Path': Path, 'np': np, 'pd': pd}
    tagged = [cell for cell in notebook['cells'] if 'eda_helpers' in cell.get('metadata', {}).get('tags', [])]
    assert len(tagged) == 1
    source = tagged[0]['source']
    # Execute only the trusted helper-definition cell from this repository's notebook.
    exec(compile(''.join(source) if isinstance(source, list) else source, str(path), 'exec'), namespace)  # noqa: S102
    return namespace


def test_release_paths_cannot_escape_project(helpers, tmp_path):
    assert helpers['safe_project_path'](tmp_path, 'data/file') == tmp_path / 'data/file'
    with pytest.raises(ValueError, match='escapes'):
        helpers['safe_project_path'](tmp_path, '../other')
    with pytest.raises(ValueError, match='relative'):
        helpers['safe_project_path'](tmp_path, str(tmp_path.resolve()))


def test_zero_denominator_stays_unknown(helpers):
    result = helpers['divide_valid'](pd.Series([10., 0., 8.]), pd.Series([2., 0., np.nan]))
    assert result.iloc[0] == 5
    assert result.iloc[1:].isna().all()


def test_history_uses_only_preceding_matching_weeks(helpers):
    index = pd.date_range('2024-01-01', periods=24 * 50, freq='h')
    values = pd.DataFrame({'zone': np.arange(len(index), dtype=float)}, index=index)
    previous, four = helpers['matching_week_forecasts'](values)
    target = index[24 * 35]
    expected = np.mean([values.loc[target - pd.Timedelta(weeks=week), 'zone'] for week in range(1, 5)])
    assert four.loc[target, 'zone'] == expected
    assert previous.loc[target, 'zone'] == values.loc[target - pd.Timedelta(weeks=1), 'zone']
    changed = values.copy()
    changed.loc[target:, 'zone'] = 1e12
    _, changed_prediction = helpers['matching_week_forecasts'](changed)
    assert changed_prediction.loc[target, 'zone'] == expected


def test_missing_historical_week_not_replaced_by_zero_or_three_week_mean(helpers):
    index = pd.date_range('2024-01-01', periods=24 * 50, freq='h')
    values = pd.DataFrame({'zone': 10.}, index=index)
    target = index[24 * 35]
    values.loc[target - pd.Timedelta(weeks=2), 'zone'] = np.nan
    previous, four = helpers['matching_week_forecasts'](values)
    assert previous.loc[target, 'zone'] == 10
    assert pd.isna(four.loc[target, 'zone'])
    assert four.iloc[:24 * 28].isna().all().all()


def test_spring_gap_is_not_shifted_into_another_civil_hour(helpers):
    utc = pd.date_range('2024-02-01', '2024-04-01', freq='h', tz='UTC')
    civil = utc.tz_convert('America/New_York').tz_localize(None)
    values = pd.DataFrame({'zone': 5.}, index=civil)
    previous, _ = helpers['matching_week_forecasts'](values)
    assert pd.isna(previous.loc['2024-03-17 02:00', 'zone'])
    assert previous.loc['2024-03-17 03:00', 'zone'] == 5


def test_duplicate_civil_hours_fail_instead_of_guessing_fold(helpers):
    values = pd.DataFrame({'zone': [1., 2.]}, index=pd.to_datetime(['2024-11-03 01:00'] * 2))
    with pytest.raises(ValueError, match='unique'):
        helpers['matching_week_forecasts'](values)


def test_h1_excludes_incomplete_and_partial_weeks(helpers):
    index = pd.date_range('2024-01-01', periods=15 * 168 + 24, freq='h')
    values = pd.DataFrame({1: 2., 2: 4., 3: 6.}, index=index)
    values.iloc[200, 1] = np.nan
    zone, hour, weeks = helpers['complete_week_matrices'](values)
    assert len(weeks) == 14
    assert zone.shape == (14, 3) and hour.shape == (14, 24)
    assert pd.Timestamp('2024-01-08') not in weeks
    assert np.allclose(zone, [2, 4, 6])
    assert np.allclose(hour, 12)


def test_insufficient_h1_weeks_fail_clearly(helpers):
    values = pd.DataFrame({1: 2.}, index=pd.date_range('2024-01-01', periods=168 * 3, freq='h'))
    with pytest.raises(ValueError, match='12 complete'):
        helpers['complete_week_matrices'](values)


def test_forecast_metrics_handle_zero_demand_and_reject_missing(helpers):
    scores = helpers['forecast_scores']([0., 2.], [1., 1.])
    assert scores['MAE_requests'] == 1 and scores['WAPE_percent'] == 100
    assert helpers['forecast_scores']([0.], [0.])['WAPE_percent'] is None
    with pytest.raises(ValueError, match='missing'):
        helpers['forecast_scores']([np.nan], [1.])
    with pytest.raises(ValueError, match='shapes'):
        helpers['forecast_scores']([], [])


def test_share_measure_and_seeded_uncertainty(helpers):
    mismatch = helpers['share_mismatch']
    assert mismatch([1, 1, 1, 1]) == 0
    assert mismatch([0, 0, 0, 4]) == .75
    with pytest.raises(ValueError, match='positive'):
        mismatch([0, 0])
    matrix = np.tile([1., 2., 3.], (14, 1))
    first = helpers['block_share_interval'](matrix, repeats=30, seed=7)
    second = helpers['block_share_interval'](matrix * 10, repeats=30, seed=7)
    assert np.allclose(first, second)
    assert first[0] <= mismatch(matrix.mean(axis=0)) <= first[1] + 1e-12


def test_bootstrap_blocks_do_not_bridge_missing_calendar_weeks(helpers):
    matrix = np.tile([1., 2., 3.], (14, 1))
    weeks = pd.date_range('2024-01-01', periods=28, freq='7D')[::2]
    with pytest.raises(ValueError, match='consecutive'):
        helpers['block_share_interval'](matrix, repeats=10, block_length=2, week_starts=weeks)
