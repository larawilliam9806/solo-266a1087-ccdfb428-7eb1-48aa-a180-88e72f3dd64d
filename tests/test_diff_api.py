"""差异分析 API 集成测试：两版快照固定、增删改、冲突、参数与版本校验。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import upload, valid_files

MONDAY = "2026-11-23"   # 2026-11-23 是周一，WKD / ALL 均有效


def _publish_two(client):
    r1 = upload(client, valid_files(), label="v1")
    assert r1.status_code == 201, r1.text
    v1 = r1.json()["version_id"]

    files2 = valid_files(
        routes="route_id,route_short_name,route_long_name,route_type\n"
               "R1,1路,中央车站-机场,3\n",
        trips="route_id,service_id,trip_id\n"
              "R1,WKD,T1\nR1,WKD,T3\nR1,WKD,TX\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   # T1 时刻调整：S2 由 08:10/08:12 改为 08:20/08:20
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,08:20:00,08:20:00,S2,2\n"
                   "T1,08:25:00,08:25:00,S3,3\n"
                   "T1,09:00:00,09:00:00,S4,4\n"
                   # T3 与旧版 T2 班次号不同、停站序列与时刻完全相同
                   "T3,10:00:00,10:00:00,S4,1\n"
                   "T3,10:30:00,10:32:00,S3,2\n"
                   "T3,10:45:00,10:45:00,S1,3\n"
                   # TX 全新班次，停站序列唯一
                   "TX,12:00:00,12:00:00,S1,1\n"
                   "TX,12:30:00,12:30:00,S2,2\n")
    r2 = upload(client, files2, label="v2")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()["version_id"]
    return v1, v2


def test_diff_added_removed_adjusted_and_renumber(client):
    v1, v2 = _publish_two(client)
    r = client.get(f"/api/diff?old_version_id={v1}&new_version_id={v2}"
                   f"&route_id=R1&date={MONDAY}")
    assert r.status_code == 200, r.text
    d = r.json()

    assert d["old_version"] == {"id": v1, "label": "v1",
                                "route_present": True,
                                "route": d["old_version"]["route"]}
    assert d["new_version"]["id"] == v2
    assert d["summary"] == {
        "old_trip_count": 2, "new_trip_count": 3,
        "added": 1, "removed": 0, "adjusted": 1,
        "unchanged": 1, "conflicts": 0}

    # 新增 TX，含完整停站
    assert [t["trip_id"] for t in d["added"]] == ["TX"]
    assert [s["stop_id"] for s in d["added"][0]["stop_times"]] == ["S1", "S2"]

    # T1 时刻调整：首个变化停站为第 2 站 S2
    adj = d["adjusted"][0]
    assert adj["matched_by"] == "trip_id"
    fc = adj["first_change"]
    assert fc["index"] == 1 and fc["change_type"] == "times"
    assert fc["old_stop"]["stop_id"] == "S2"
    assert fc["new_stop"]["stop_id"] == "S2"
    assert fc["old_stop"]["arrival"] == "08:10:00"
    assert fc["new_stop"]["arrival"] == "08:20:00"
    assert fc["arrival_delta_secs"] == 600
    assert fc["arrival_delta_minutes"] == 10
    assert fc["departure_delta_secs"] == 480     # 08:12 → 08:20
    assert fc["departure_delta_minutes"] == 8

    # T2 → T3 班次号变化，按停站序列匹配且未变化
    assert d["unchanged"] == [{
        "matched_by": "stop_sequence", "old_trip_id": "T2", "new_trip_id": "T3"}]


def test_diff_route_missing_in_one_version(client):
    v1, v2 = _publish_two(client)
    # R2 在 v1 存在、v2 已删除：整版结果仍完整，N1 计为移除
    r = client.get(f"/api/diff?old_version_id={v1}&new_version_id={v2}"
                   f"&route_id=R2&date={MONDAY}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["old_version"]["route_present"] is True
    assert d["new_version"]["route_present"] is False and d["new_version"]["route"] is None
    assert d["summary"]["removed"] == 1
    assert d["removed"][0]["trip_id"] == "N1"
    assert d["summary"]["added"] == 0


def test_diff_route_missing_in_both_is_404(client):
    v1, v2 = _publish_two(client)
    r = client.get(f"/api/diff?old_version_id={v1}&new_version_id={v2}"
                   f"&route_id=R9&date={MONDAY}")
    assert r.status_code == 404


def test_diff_conflict_lists_candidates(client):
    v1, v2 = _publish_two(client)
    # v3：两个新班次 A1/A2 都与旧版 T1 的停站序列 [S1,S2,S3,S4] 相同，
    # 班次号均对不上 → 序列候选不唯一，必须报冲突
    files3 = valid_files(
        routes="route_id,route_short_name,route_long_name,route_type\n"
               "R1,1路,中央车站-机场,3\n",
        trips="route_id,service_id,trip_id\nR1,WKD,A1\nR1,WKD,A2\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "A1,08:00:00,08:00:00,S1,1\n"
                   "A1,08:10:00,08:12:00,S2,2\n"
                   "A1,08:25:00,08:25:00,S3,3\n"
                   "A1,09:00:00,09:00:00,S4,4\n"
                   "A2,11:00:00,11:00:00,S1,1\n"
                   "A2,11:10:00,11:12:00,S2,2\n"
                   "A2,11:25:00,11:25:00,S3,3\n"
                   "A2,12:00:00,12:00:00,S4,4\n")
    v3 = upload(client, files3, label="v3").json()["version_id"]

    r = client.get(f"/api/diff?old_version_id={v1}&new_version_id={v3}"
                   f"&route_id=R1&date={MONDAY}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["summary"]["conflicts"] == 1
    c = d["conflicts"][0]
    assert c["signature"] == ["S1", "S2", "S3", "S4"]
    assert [t["trip_id"] for t in c["old_trips"]] == ["T1"]
    assert [t["trip_id"] for t in c["new_trips"]] == ["A1", "A2"]
    # 冲突班次不并入增删改；T2 序列唯一单侧出现，正常计移除
    assert d["summary"]["removed"] == 1
    assert d["removed"][0]["trip_id"] == "T2"
    assert d["summary"]["added"] == 0
    assert d["summary"]["adjusted"] == 0


def test_diff_snapshots_are_pinned_and_immutable(client):
    v1, v2 = _publish_two(client)
    # 当前正式版已切到 v2，但指定 v1↔v2 的差异仍能读到 v1 的完整旧数据
    cur = client.get("/api/versions/current").json()["id"]
    assert cur == v2
    d = client.get(f"/api/diff?old_version_id={v1}&new_version_id={v2}"
                   f"&route_id=R1&date={MONDAY}").json()
    assert d["old_version"]["id"] == v1
    assert d["summary"]["old_trip_count"] == 2


def test_diff_rejects_same_version(client):
    v1, _ = _publish_two(client)
    r = client.get(f"/api/diff?old_version_id={v1}&new_version_id={v1}"
                   f"&route_id=R1&date={MONDAY}")
    assert r.status_code == 422


def test_diff_rejects_unknown_and_failed_version(client):
    v1, v2 = _publish_two(client)
    assert client.get(f"/api/diff?old_version_id=9999&new_version_id={v2}"
                      f"&route_id=R1&date={MONDAY}").status_code == 404
    assert client.get(f"/api/diff?old_version_id={v1}&new_version_id=9999"
                      f"&route_id=R1&date={MONDAY}").status_code == 404

    # 制造一个失败版本
    bad = valid_files(trips="route_id,service_id,trip_id\nR9,WKD,T1\n")
    failed = upload(client, bad, label="bad")
    assert failed.status_code == 422
    fv = failed.json()["detail"]["version_id"]
    r = client.get(f"/api/diff?old_version_id={fv}&new_version_id={v2}"
                   f"&route_id=R1&date={MONDAY}")
    assert r.status_code == 422
    assert "已发布" in r.json()["detail"]


def test_diff_rejects_bad_date(client):
    v1, v2 = _publish_two(client)
    r = client.get(f"/api/diff?old_version_id={v1}&new_version_id={v2}"
                   f"&route_id=R1&date=2026-13-40")
    assert r.status_code == 422


def test_diff_respects_operating_day(client):
    v1, v2 = _publish_two(client)
    # 周六：R1 的 WKD 服务不运营，两侧均无班次 → 全零结果而非报错
    r = client.get(f"/api/diff?old_version_id={v1}&new_version_id={v2}"
                   f"&route_id=R1&date=2026-11-21")
    assert r.status_code == 200, r.text
    s = r.json()["summary"]
    assert s["old_trip_count"] == 0 and s["new_trip_count"] == 0
    assert s["added"] == s["removed"] == s["adjusted"] == s["conflicts"] == 0


def test_version_routes_endpoint(client):
    v1, v2 = _publish_two(client)
    r1 = client.get(f"/api/versions/{v1}/routes")
    assert r1.status_code == 200
    assert {r["route_id"] for r in r1.json()["routes"]} == {"R1", "R2"}
    r2 = client.get(f"/api/versions/{v2}/routes")
    assert {r["route_id"] for r in r2.json()["routes"]} == {"R1"}
    assert client.get("/api/versions/9999/routes").status_code == 404
