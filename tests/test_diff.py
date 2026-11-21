"""差异引擎单元测试：直接构造快照，不经过数据库与 HTTP。

覆盖：
- trip_id 匹配：未变化 / 时刻调整（首个变化停站、分钟差）/ 停站序列变化
- 班次号变化：按有序停站序列匹配
- 候选不唯一：报冲突并列出对应班次，不合并、不计入增删改
- 新增 / 移除 / 一侧无班次
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.diff import Snapshot, StopCall, TripView, diff_snapshots


def call(seq, stop, arr_hhmm, dep_hhmm=None, name=None):
    def secs(hhmm):
        parts = [int(x) for x in hhmm.split(":")]
        h, m = parts[0], parts[1]
        s = parts[2] if len(parts) > 2 else 0
        return h * 3600 + m * 60 + s
    return StopCall(seq, stop, name or f"{stop}站",
                    secs(arr_hhmm), secs(dep_hhmm or arr_hhmm))


def trip(tid, stops, times, service="WKD", headsign="", direction=0):
    if times and isinstance(times[0], tuple):
        calls = [call(i + 1, s, arr, dep)
                 for i, (s, (arr, dep)) in enumerate(zip(stops, times))]
    else:
        calls = [call(i + 1, s, t, t) for i, (s, t) in enumerate(zip(stops, times))]
    return TripView(tid, service, headsign, direction, calls)


def snap(vid, trips, route_present=True):
    return Snapshot(vid, f"v{vid}",
                    {"route_id": "R1"} if route_present else None, trips)


def run(old_trips, new_trips):
    return diff_snapshots(snap(1, old_trips), snap(2, new_trips),
                          "R1", "2026-11-23", "monday")


# ---------- 无变化 ----------

def test_identical_snapshots_unchanged():
    t = trip("T1", ["S1", "S2"], ["08:00", "08:10"])
    r = run([t], [trip("T1", ["S1", "S2"], ["08:00", "08:10"])])
    assert r["summary"] == {
        "old_trip_count": 1, "new_trip_count": 1,
        "added": 0, "removed": 0, "adjusted": 0,
        "unchanged": 1, "conflicts": 0}
    assert r["unchanged"][0]["matched_by"] == "trip_id"


# ---------- 新增 / 移除 ----------

def test_added_and_removed():
    old = [trip("T1", ["S1", "S2"], ["08:00", "08:10"])]
    new = [trip("T1", ["S1", "S2"], ["08:00", "08:10"]),
           trip("T9", ["S1", "S3"], ["09:00", "09:20"])]
    r = run(old, new)
    assert r["summary"]["added"] == 1 and r["added"][0]["trip_id"] == "T9"
    assert r["summary"]["removed"] == 0

    r2 = run(new, old)
    assert r2["summary"]["removed"] == 1 and r2["removed"][0]["trip_id"] == "T9"
    assert r2["summary"]["added"] == 0


def test_one_side_empty_is_all_added_or_removed():
    r = run([], [trip("T1", ["S1"], ["08:00"])])
    assert r["summary"]["added"] == 1 and r["summary"]["removed"] == 0
    r2 = run([trip("T1", ["S1"], ["08:00"])], [])
    assert r2["summary"]["removed"] == 1 and r2["summary"]["added"] == 0


# ---------- trip_id 匹配 + 时刻调整 ----------

def test_time_adjustment_first_change_and_minutes():
    old = trip("T1", ["S1", "S2", "S3"], ["08:00", "08:10", "08:30"])
    # 第一站不变，第二站发车晚 5 分钟、到站晚 5 分钟
    new = trip("T1", ["S1", "S2", "S3"], ["08:00", "08:15", "08:35"])
    r = run([old], [new])
    assert r["summary"]["adjusted"] == 1
    a = r["adjusted"][0]
    fc = a["first_change"]
    assert fc["index"] == 1                       # 第二个停站（0 基）
    assert fc["change_type"] == "times"
    assert fc["old_stop"]["stop_id"] == "S2"
    assert fc["arrival_delta_secs"] == 300
    assert fc["arrival_delta_minutes"] == 5
    assert fc["departure_delta_minutes"] == 5


def test_negative_delta_and_seconds_fraction():
    old = trip("T1", ["S1", "S2"],
               [("08:00:00", "08:00:00"), ("08:10:00", "08:10:00")])
    # 第一站到站 -90 秒、发车 -90 秒；第二站 -60 秒
    new = trip("T1", ["S1", "S2"],
               [("07:58:30", "07:58:30"), ("08:09:00", "08:09:00")])
    r = run([old], [new])
    fc = r["adjusted"][0]["first_change"]
    assert fc["index"] == 0
    assert fc["arrival_delta_secs"] == -90
    assert fc["arrival_delta_minutes"] == -1.5
    assert fc["departure_delta_minutes"] == -1.5


def test_stop_sequence_change_is_stop_change_without_minutes():
    old = trip("T1", ["S1", "S2"], ["08:00", "08:10"])
    new = trip("T1", ["S1", "S9"], ["08:00", "08:12"])
    r = run([old], [new])
    fc = r["adjusted"][0]["first_change"]
    assert fc["change_type"] == "stop"
    assert fc["old_stop"]["stop_id"] == "S2"
    assert fc["new_stop"]["stop_id"] == "S9"
    assert fc["arrival_delta_minutes"] is None


def test_extra_stop_marks_missing_side_null():
    old = trip("T1", ["S1"], ["08:00"])
    new = trip("T1", ["S1", "S2"], ["08:00", "08:10"])
    fc = run([old], [new])["adjusted"][0]["first_change"]
    assert fc["change_type"] == "stop"
    assert fc["old_stop"] is None and fc["new_stop"]["stop_id"] == "S2"


# ---------- 班次号变化：有序停站序列匹配 ----------

def test_renumbered_trip_matched_by_signature():
    old = trip("T1", ["S1", "S2", "S3"], ["08:00", "08:10", "08:30"])
    new = trip("T100", ["S1", "S2", "S3"], ["08:00", "08:15", "08:30"])
    r = run([old], [new])
    assert r["summary"]["added"] == 0 and r["summary"]["removed"] == 0
    assert r["summary"]["adjusted"] == 1
    a = r["adjusted"][0]
    assert a["matched_by"] == "stop_sequence"
    assert a["old_trip"]["trip_id"] == "T1" and a["new_trip"]["trip_id"] == "T100"
    assert a["first_change"]["index"] == 1


def test_renumbered_identical_trip_is_unchanged():
    old = trip("T1", ["S1", "S2"], ["08:00", "08:10"])
    new = trip("T100", ["S1", "S2"], ["08:00", "08:10"])
    r = run([old], [new])
    assert r["summary"]["unchanged"] == 1
    assert r["unchanged"][0] == {
        "matched_by": "stop_sequence", "old_trip_id": "T1", "new_trip_id": "T100"}


def test_different_signatures_are_added_and_removed():
    old = trip("T1", ["S1", "S2"], ["08:00", "08:10"])
    new = trip("T100", ["S1", "S3"], ["08:00", "08:10"])
    r = run([old], [new])
    assert r["summary"]["removed"] == 1 and r["summary"]["added"] == 1
    assert r["summary"]["adjusted"] == 0 and r["summary"]["conflicts"] == 0


# ---------- 冲突：候选不唯一 ----------

def test_conflict_when_new_signature_not_unique():
    old = [trip("T1", ["S1", "S2"], ["08:00", "08:10"])]
    new = [trip("T2", ["S1", "S2"], ["09:00", "09:10"]),
           trip("T3", ["S1", "S2"], ["10:00", "10:10"])]
    r = run(old, new)
    assert r["summary"]["conflicts"] == 1
    c = r["conflicts"][0]
    assert c["signature"] == ["S1", "S2"]
    assert [t["trip_id"] for t in c["old_trips"]] == ["T1"]
    assert [t["trip_id"] for t in c["new_trips"]] == ["T2", "T3"]
    # 冲突班次不计入新增/移除/调整
    assert r["summary"]["added"] == 0
    assert r["summary"]["removed"] == 0
    assert r["summary"]["adjusted"] == 0


def test_conflict_when_old_signature_not_unique():
    old = [trip("T1", ["S1", "S2"], ["08:00", "08:10"]),
           trip("T2", ["S1", "S2"], ["09:00", "09:10"])]
    new = [trip("T3", ["S1", "S2"], ["10:00", "10:10"])]
    r = run(old, new)
    assert r["summary"]["conflicts"] == 1
    assert len(r["conflicts"][0]["old_trips"]) == 2
    assert len(r["conflicts"][0]["new_trips"]) == 1


def test_conflict_does_not_poison_other_trips():
    # 一组冲突 + 一组正常序列匹配，互不影响
    old = [trip("T1", ["S1", "S2"], ["08:00", "08:10"]),
           trip("T8", ["S5", "S6"], ["07:00", "07:10"])]
    new = [trip("T2", ["S1", "S2"], ["09:00", "09:10"]),
           trip("T3", ["S1", "S2"], ["10:00", "10:10"]),
           trip("T9", ["S5", "S6"], ["07:00", "07:15"])]
    r = run(old, new)
    assert r["summary"]["conflicts"] == 1
    assert r["summary"]["adjusted"] == 1
    assert r["adjusted"][0]["old_trip"]["trip_id"] == "T8"


def test_id_match_takes_precedence_over_signature_group():
    # T1 在两侧 id 相同 → 直接按 id 匹配，不参与序列分组，
    # 因此同序列的 T2/T3 成为唯一 1:1 序列候选，按序列匹配而非报冲突
    old = [trip("T1", ["S1", "S2"], ["08:00", "08:10"]),
           trip("T2", ["S1", "S2"], ["09:00", "09:10"])]
    new = [trip("T1", ["S1", "S2"], ["08:00", "08:10"]),
           trip("T3", ["S1", "S2"], ["10:00", "10:10"])]
    r = run(old, new)
    assert r["summary"]["conflicts"] == 0
    assert r["summary"]["unchanged"] == 1
    assert r["unchanged"][0]["matched_by"] == "trip_id"
    assert r["summary"]["adjusted"] == 1
    assert r["adjusted"][0]["matched_by"] == "stop_sequence"
    assert (r["adjusted"][0]["old_trip"]["trip_id"],
            r["adjusted"][0]["new_trip"]["trip_id"]) == ("T2", "T3")


def test_id_precedence_leaves_true_added_and_removed():
    # 序列不同的单侧班次仍正常计新增/移除，不受 id 匹配影响
    old = [trip("T1", ["S1", "S2"], ["08:00", "08:10"]),
           trip("T2", ["S5", "S6"], ["09:00", "09:10"])]
    new = [trip("T1", ["S1", "S2"], ["08:00", "08:10"]),
           trip("T3", ["S7", "S8"], ["10:00", "10:10"])]
    r = run(old, new)
    assert r["summary"]["conflicts"] == 0
    assert [t["trip_id"] for t in r["removed"]] == ["T2"]
    assert [t["trip_id"] for t in r["added"]] == ["T3"]
