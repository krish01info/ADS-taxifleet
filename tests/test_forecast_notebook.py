"""Verify forecasts need no recent completed trips or future outcomes."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.preprocessing import OneHotEncoder


@pytest.fixture(scope='module')
def helpers():
    """Load only trusted notebook helper definitions, never data/training cells."""
    path = Path(__file__).resolve().parents[1] / 'notebooks/03_forecast.ipynb'
    nb = json.loads(path.read_text(encoding='utf-8'))
    namespace = {'Path': Path, 'np': np, 'pd': pd}
    cells = [c for c in nb['cells'] if 'forecast_helpers' in c.get('metadata', {}).get('tags', [])]
    assert len(cells) == 1
    source = cells[0]['source']
    exec(compile(''.join(source) if isinstance(source,list) else source,str(path),'exec'),namespace)  # noqa: S102
    return namespace


def locations():
    return pd.DataFrame({'zone_id':[1,2], 'centroid_latitude':[40.7,40.8],
        'centroid_longitude':[-73.9,-73.8], 'is_airport':[False,True], 'borough':['A','B']})


def schedule(start='2026-12-01 05:00', hours=48):
    index = pd.MultiIndex.from_product([pd.date_range(start,periods=hours,freq='h',tz='UTC'),[1,2]],
                                       names=['hour_start_utc','zone_id'])
    rows=index.to_frame(index=False)
    rows['is_federal_holiday']=False
    rows['has_verified_holiday_calendar']=True
    return rows


def test_future_schedule_requires_no_request_counts(helpers):
    rows=schedule()
    features=helpers['build_known_features'](rows,locations())
    assert len(features)==len(rows)
    assert 'request_count' not in features
    assert np.isfinite(features[helpers['NUMERIC_FEATURES']]).all().all()
    assert features['event_date_nyc'].min()==pd.Timestamp('2026-12-01')


def test_current_past_future_and_trip_outcomes_are_ignored(helpers):
    rows=schedule()
    original=helpers['build_known_features'](rows,locations())
    for field in ['request_count','lag_1h','mean_four_matching_weeks','trip_seconds','base_fare_usd','temperature_c']:
        rows[field]=np.arange(len(rows))*1e9
    changed=helpers['build_known_features'](rows,locations())
    pd.testing.assert_frame_equal(original,changed)
    assert not any('lag' in field or 'request' in field for field in helpers['NUMERIC_FEATURES'])


def test_same_trained_model_predictions_ignore_added_outcomes(helpers):
    rows=schedule(hours=96)
    feature=helpers['build_known_features'](rows,locations())
    encoder=OneHotEncoder(categories=[['A','B']],sparse_output=False,dtype=np.float32).fit(feature[['borough']])
    encoded=helpers['encode_features'](encoder,feature)
    rows['request_count']=1e12
    changed=helpers['encode_features'](encoder,helpers['build_known_features'](rows,locations()))
    assert np.array_equal(encoded,changed)
    target=np.arange(len(rows))%30
    for model in [RandomForestRegressor(n_estimators=2,max_depth=3,random_state=7),
                  GradientBoostingRegressor(n_estimators=2,max_depth=2,random_state=7)]:
        model.fit(encoded,target)
        expected,_=helpers['predict_nonnegative'](model,encoded)
        actual,_=helpers['predict_nonnegative'](model,changed)
        assert np.array_equal(actual,expected)


def test_calendar_and_geography_are_required_not_guessed(helpers):
    rows=schedule()
    rows.loc[0,'has_verified_holiday_calendar']=False
    with pytest.raises(ValueError,match='holiday'):
        helpers['build_known_features'](rows,locations())
    rows=schedule(); rows.loc[0,'zone_id']=999
    with pytest.raises(ValueError,match='geography'):
        helpers['build_known_features'](rows,locations())


def test_dst_fold_uses_real_instants_without_request_history(helpers):
    rows=schedule(start='2024-11-03 05:00',hours=2)
    feature=helpers['build_known_features'](rows,locations())
    assert feature['hour_start_utc'].nunique()==2
    assert set(feature['hour'])=={1.0}
    assert len(feature)==4


def test_duplicate_or_naive_schedule_fails(helpers):
    rows=schedule()
    with pytest.raises(ValueError,match='unique'):
        helpers['build_known_features'](pd.concat([rows,rows.iloc[:1]]),locations())
    rows['hour_start_utc']=rows['hour_start_utc'].dt.tz_localize(None)
    with pytest.raises(ValueError,match='timezone-aware'):
        helpers['build_known_features'](rows,locations())


def training_reference():
    return pd.DataFrame({'zone_id':[1]*4,
        'hour_start_utc':pd.to_datetime(['2024-08-05 13:00Z','2024-08-12 13:00Z','2024-08-19 13:00Z','2024-08-26 13:00Z']),
        'request_count':[2.,4.,6.,8.]})


def test_frozen_profiles_are_training_only_and_correct(helpers):
    training=training_reference()
    profile=helpers['build_frozen_profiles'](training,'2024-09-01')
    assert profile.iloc[0]['last_training_week']==8
    assert profile.iloc[0]['four_training_weeks']==5
    training.loc[0,'hour_start_utc']=pd.Timestamp('2024-09-02 13:00Z')
    with pytest.raises(ValueError,match='validation/test'):
        helpers['build_frozen_profiles'](training,'2024-09-01')


def test_frozen_profile_does_not_update_from_scoring_labels(helpers):
    profile=helpers['build_frozen_profiles'](training_reference(),'2024-09-01')
    rows=schedule(start='2024-11-04 14:00',hours=1)
    frame=helpers['build_known_features'](rows,locations())
    first=helpers['attach_frozen_profiles'](frame,profile)
    frame['request_count']=1e12
    second=helpers['attach_frozen_profiles'](frame,profile)
    pd.testing.assert_series_equal(first['four_training_weeks'],second['four_training_weeks'])
    assert first.iloc[0]['four_training_weeks']==5


def test_missing_reference_observation_is_not_three_week_mean(helpers):
    rows=training_reference();rows.loc[0,'request_count']=np.nan
    profile=helpers['build_frozen_profiles'](rows,'2024-09-01')
    assert pd.isna(profile.iloc[0]['four_training_weeks'])
    assert profile.iloc[0]['last_training_week']==8


def test_labels_do_not_control_input_creation(helpers):
    rows=schedule();rows['request_count']=np.nan
    labelled=helpers['build_features'](rows,locations())
    assert len(labelled)==len(rows) and not labelled['is_usable'].any()
    assert np.isfinite(labelled[helpers['NUMERIC_FEATURES']]).all().all()


def test_count_metrics_and_bias_calibration(helpers):
    assert helpers['score_counts']([0,2],[1,1])['WAPE_percent']==100
    assert helpers['score_counts']([0],[0])['WAPE_percent'] is None
    assert helpers['count_bias_factor']([2,4],[1,2])==2
    with pytest.raises(ValueError,match='all-zero'):
        helpers['count_bias_factor']([1],[0])
    with pytest.raises(ValueError,match='finite'):
        helpers['score_counts']([1],[np.nan])
