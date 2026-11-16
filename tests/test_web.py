"""Web API tests: the FastAPI Report endpoint must give the same numbers as the engine."""
import os
import sys

import pytest

pytest.importorskip('fastapi')
pytest.importorskip('httpx')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CSV = os.environ.get('SP500_CSV') or os.path.join(ROOT, 'sp500.csv')
if not os.path.exists(CSV):
    pytest.skip('sp500.csv not found', allow_module_level=True)
os.environ['BEHAVIOUR_MODEL_CSV'] = CSV          # no network in tests

from fastapi.testclient import TestClient  # noqa: E402

from app import data, engine  # noqa: E402
from web.server import app  # noqa: E402

client = TestClient(app)
NO_EXTRAS = {k: False for k, _, _ in engine.ALL_SWITCHES}


def post(**kw):
    body = dict(symbol='^GSPC', date='2025-02-18', side='long', follow='same', horizon=252, switches=NO_EXTRAS)
    body.update(kw)
    return client.post('/api/report', json=body)


def test_page_and_meta_load():
    assert client.get('/').status_code == 200 and 'Retail Behaviour Model' in client.get('/').text
    meta = client.get('/api/meta').json()
    assert len(meta['profiles']) == 10 and len(meta['securities']) == 15
    assert [s['key'] for s in meta['switches'] if s['default']] == [k for k, _, _ in engine.SWITCHES]
    assert 'Not financial advice' in meta['disclaimer']


def test_known_report_case_over_the_api():
    rep = post().json()
    by = {r['name']: r for r in rep['results']}
    assert by['Disciplined']['ntr'] == 30 and by['Disciplined']['result'] == pytest.approx(0.109, abs=0.002)
    assert by['All combined']['wiped'] and '2025-04-01' <= by['All combined']['trades'][-1]['exit'] <= '2025-04-10'
    assert 'Not financial advice' in rep['disclaimer']


@pytest.mark.parametrize('follow', ['same', 'trend', 'chase', 'random_one'])
def test_api_equals_engine(follow):
    rows = data.load_csv(CSV)
    dates, closes = [d for d, _ in rows], [c for _, c in rows]
    switches = dict(avg_down=True, move_stop=True, panic=True, give_back=True, break_even=True, top_up=True)
    rep = post(follow=follow, trader=5, side='short', date='2025-11-12', switches=switches).json()
    profs = [p.with_switches(**switches) for p in engine.PRESETS]
    want = engine.run_report(closes, dates, dates.index('2025-11-12'), 'short', follow, 252, profs, trader=5)
    for got, exp in zip(rep['results'], want['results']):
        assert got['curve'] == exp['curve'] and got['opp_curve'] == exp['opp_curve']
        assert (got['ntr'], got['wins'], got['wiped'], got['deposited']) == (exp['ntr'], exp['wins'], exp['wiped'], exp['deposited'])
        assert [t['exit'] for t in got['trades']] == [t.exit_date for t in exp['trades']]


def test_median_view():
    rep = post(follow='random').json()
    r = rep['results'][8]
    assert r['random'] and r['trades'] is None and 0 <= r['lose'] <= 1 and len(r['curve']) == len(rep['dates'])


def test_bad_requests_get_clear_messages():
    assert 'not a trading day' in post(date='2025-02-16').json()['detail']
    assert 'latest close' in post(date=data.load_csv(CSV)[-1][0]).json()['detail']
    assert post(symbol='../etc/passwd').status_code == 400
    assert post(follow='nonsense').status_code == 400
    assert post(side='sideways').status_code == 422
    assert post(date='yesterday').status_code == 422
