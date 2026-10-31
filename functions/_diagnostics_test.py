"""Küçük, bağımlılık gerektirmeyen tanılama sözleşmesi testleri."""

from types import SimpleNamespace

import firestore_logger
from firestore_logger import _tanilama_olustur, _yanita_tanilama_ekle
from hedef_hesaplayici import HedefHesaplayici
from solver_models import SolverGorev


def test_client_trace_preserved():
    trace = _tanilama_olustur(
        "nobet_kapasite",
        {"tanilama": {"denemeId": "d-1", "istekId": "r-2", "istemciSurum": "web-7"}},
        {"basari": False, "durum": "INFEASIBLE", "asama": "KAPASITE"},
        123,
    )
    assert trace["denemeId"] == "d-1"
    assert trace["istekId"] == "r-2"
    assert trace["serverSureMs"] == 123
    assert trace["durum"] == "INFEASIBLE"
    assert trace["asama"] == "KAPASITE"
    assert "kayitId" not in trace


def test_response_gets_real_record_id_only_when_supplied():
    result = {"basari": False}
    _yanita_tanilama_ekle(result, {"endpoint": "x", "kayitId": "real-id"})
    assert result["tanilama"]["kayitId"] == "real-id"


def test_no_client_trace_does_not_invent_ids():
    trace = _tanilama_olustur("nobet_coz", {}, None, 7)
    assert "denemeId" not in trace
    assert "istekId" not in trace
    assert "kayitId" not in trace


def test_role_quota_metadata_counts_real_slots():
    hesap = object.__new__(HedefHesaplayici)
    hesap.gun_sayisi = 30
    hesap.tip_sayilari = {"hici": 20, "prs": 4, "cum": 2, "cmt": 2, "pzr": 2}
    hesap.gorevler = [
        SolverGorev(id=1, ad="AMELİYATHANE #1", base_name="AMELİYATHANE", slot_idx=0),
        SolverGorev(id=2, ad="AMELİYATHANE #2", base_name="AMELİYATHANE", slot_idx=1),
        SolverGorev(id=3, ad="AMELİYATHANE #3", base_name="AMELİYATHANE", slot_idx=2),
        SolverGorev(id=4, ad="KVC", base_name="KVC", slot_idx=3),
    ]
    kotalar = hesap._hesapla_gorev_kotalari()
    assert kotalar["AMELİYATHANE"]["toplam"] == 90
    assert kotalar["AMELİYATHANE"]["tip_dagilimi"]["cum"] == 6
    assert kotalar["KVC"]["toplam"] == 30


class _FakeDoc:
    id = "debug-real-1"

    def __init__(self, fail=False):
        self.fail = fail

    def collection(self, _name):
        raise AssertionError("test payload does not need chunks")

    def set(self, _data):
        if self.fail:
            raise RuntimeError("firestore kapalı")


class _FakeCollection:
    def __init__(self, fail=False):
        self.fail = fail

    def document(self):
        return _FakeDoc(self.fail)


class _FakeDb:
    def __init__(self, fail=False):
        self.fail = fail

    def collection(self, _name):
        return _FakeCollection(self.fail)


def test_log_success_adds_real_record_id(monkeypatch=None):
    import firebase_admin
    original = getattr(firebase_admin, "firestore", None)
    firebase_admin.firestore = SimpleNamespace(client=lambda: _FakeDb(False))
    try:
        result = {"basari": True, "durum": "FEASIBLE", "asama": "FINAL", "atamalar": []}
        firestore_logger.log_session("nobet_coz", {"tanilama": {"istekId": "r1"}}, result, 9)
        assert result["tanilama"]["istekId"] == "r1"
        assert result["tanilama"]["kayitId"] == "debug-real-1"
    finally:
        if original is None:
            delattr(firebase_admin, "firestore")
        else:
            firebase_admin.firestore = original


def test_log_failure_does_not_add_record_id():
    import firebase_admin
    original = getattr(firebase_admin, "firestore", None)
    firebase_admin.firestore = SimpleNamespace(client=lambda: _FakeDb(True))
    try:
        result = {"basari": False, "durum": "INFEASIBLE", "asama": "KAPASITE"}
        firestore_logger.log_session("nobet_kapasite", {"tanilama": {"denemeId": "d1"}}, result, 11)
        assert result["tanilama"]["denemeId"] == "d1"
        assert "kayitId" not in result["tanilama"]
    finally:
        if original is None:
            delattr(firebase_admin, "firestore")
        else:
            firebase_admin.firestore = original


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("*** DIAGNOSTICS TESTS PASSED ***")
