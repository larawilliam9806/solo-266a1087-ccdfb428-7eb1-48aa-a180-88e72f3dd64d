"""运行图差异分析：固定两个已发布版本的完整快照并比较班次。

匹配规则（需求固定）：
1. 先按 trip_id 一一匹配（版本内 trip_id 唯一，必为一对一）。
2. trip_id 匹配不上的班次，按「线路 + 运营日 + 有序停站序列」
   （即按 stop_sequence 排好的 stop_id 元组）匹配：
   - 两侧候选都恰好 1 个 → 匹配；
   - 任一侧候选不唯一 → 报冲突，列出两侧全部对应班次，不擅自合并，
     且这些班次不再计入新增 / 移除 / 调整；
   - 序列只在一侧出现 → 新增或移除。
3. 匹配上的班次逐站比较停站与到发时刻：完全一致为未变化，否则为调整，
   并给出首个变化停站的旧/新到发时刻与变化分钟。

整个模块不依赖 FastAPI：快照加载只吃 sqlite 连接，diff_snapshots 是纯函数，
便于单元测试。版本数据不可变，请求开始时确定两个 version_id 即固定了两版
完整快照，后续不会读到半新半旧的数据。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .gtfs import format_gtfs_time


@dataclass
class StopCall:
    """一次停站（有序停站序列中的一个元素）。"""
    stop_sequence: int
    stop_id: str
    stop_name: str
    arrival_secs: int
    departure_secs: int


@dataclass
class TripView:
    trip_id: str
    service_id: str
    headsign: str
    direction_id: int | None
    stop_times: list[StopCall] = field(default_factory=list)

    @property
    def signature(self) -> tuple[str, ...]:
        """有序停站序列：匹配用的指纹。"""
        return tuple(c.stop_id for c in self.stop_times)

    @property
    def first_departure_secs(self) -> int:
        return self.stop_times[0].departure_secs if self.stop_times else 0


@dataclass
class Snapshot:
    """某版本在指定线路 + 运营日上的完整不可变快照。"""
    version_id: int
    label: str
    route: dict | None          # 该版本中线路不存在时为 None
    trips: list[TripView] = field(default_factory=list)


# ---------------- 快照加载 ----------------

def load_snapshot(
    conn: sqlite3.Connection,
    version_id: int,
    route_id: str,
    date: str,
    weekday_col: str,
) -> Snapshot:
    """读取某版本在线路 + 运营日上的完整快照。

    调用方需先确认版本存在且已发布。线路在该版本中不存在时 route 为 None、
    trips 为空（另一版的班次将整体计为新增/移除，这仍是完整结果）。
    """
    vrow = conn.execute(
        "SELECT id, label FROM versions WHERE id = ?", (version_id,)).fetchone()
    route = conn.execute(
        "SELECT route_id, route_short_name, route_long_name, route_type "
        "FROM routes WHERE version_id = ? AND route_id = ?",
        (version_id, route_id)).fetchone()
    if route is None:
        return Snapshot(version_id=version_id, label=vrow["label"],
                        route=None, trips=[])

    services = conn.execute(
        f"SELECT service_id FROM calendar WHERE version_id = ? "
        f"AND start_date <= ? AND end_date >= ? AND {weekday_col} = 1",
        (version_id, date, date)).fetchall()
    service_ids = [r["service_id"] for r in services]

    trips: list[TripView] = []
    if service_ids:
        placeholders = ",".join("?" for _ in service_ids)
        trows = conn.execute(
            f"SELECT trip_id, service_id, trip_headsign, direction_id "
            f"FROM trips WHERE version_id = ? AND route_id = ? "
            f"AND service_id IN ({placeholders})",
            (version_id, route_id, *service_ids)).fetchall()
        for t in trows:
            srows = conn.execute(
                "SELECT st.stop_sequence, st.stop_id, s.stop_name, "
                "st.arrival_secs, st.departure_secs "
                "FROM stop_times st JOIN stops s "
                "  ON s.version_id = st.version_id AND s.stop_id = st.stop_id "
                "WHERE st.version_id = ? AND st.trip_id = ? "
                "ORDER BY st.stop_sequence",
                (version_id, t["trip_id"])).fetchall()
            calls = [StopCall(stop_sequence=r["stop_sequence"], stop_id=r["stop_id"],
                              stop_name=r["stop_name"],
                              arrival_secs=r["arrival_secs"],
                              departure_secs=r["departure_secs"]) for r in srows]
            trips.append(TripView(trip_id=t["trip_id"], service_id=t["service_id"],
                                  headsign=t["trip_headsign"],
                                  direction_id=t["direction_id"],
                                  stop_times=calls))
    trips.sort(key=lambda tr: tr.first_departure_secs)
    return Snapshot(version_id=version_id, label=vrow["label"],
                    route=dict(route), trips=trips)


# ---------------- 序列化 ----------------

def _stop_dict(c: StopCall | None) -> dict | None:
    if c is None:
        return None
    return {
        "stop_sequence": c.stop_sequence,
        "stop_id": c.stop_id,
        "stop_name": c.stop_name,
        "arrival": format_gtfs_time(c.arrival_secs),
        "departure": format_gtfs_time(c.departure_secs),
        "arrival_secs": c.arrival_secs,
        "departure_secs": c.departure_secs,
        "arrival_day_offset": c.arrival_secs // 86400,
        "departure_day_offset": c.departure_secs // 86400,
        "arrival_clock": format_gtfs_time(c.arrival_secs % 86400),
        "departure_clock": format_gtfs_time(c.departure_secs % 86400),
    }


def _trip_dict(t: TripView) -> dict:
    return {
        "trip_id": t.trip_id,
        "service_id": t.service_id,
        "headsign": t.headsign,
        "direction_id": t.direction_id,
        "first_departure_secs": t.first_departure_secs,
        "stop_times": [_stop_dict(c) for c in t.stop_times],
    }


# ---------------- 差异引擎（纯函数） ----------------

def _first_change(old: TripView, new: TripView) -> dict:
    """找匹配班次的首个变化停站。

    同序号位置停站不同（或缺一站）→ change_type='stop'，不计算分钟差；
    停站相同但到发时刻不同 → change_type='times'，给出旧/新时刻与分钟差。
    """
    for i in range(max(len(old.stop_times), len(new.stop_times))):
        oc = old.stop_times[i] if i < len(old.stop_times) else None
        nc = new.stop_times[i] if i < len(new.stop_times) else None
        if oc is None or nc is None or oc.stop_id != nc.stop_id:
            return {
                "index": i,
                "change_type": "stop",
                "old_stop": _stop_dict(oc),
                "new_stop": _stop_dict(nc),
                "arrival_delta_secs": None,
                "departure_delta_secs": None,
                "arrival_delta_minutes": None,
                "departure_delta_minutes": None,
            }
        arr_d = nc.arrival_secs - oc.arrival_secs
        dep_d = nc.departure_secs - oc.departure_secs
        if arr_d != 0 or dep_d != 0:
            return {
                "index": i,
                "change_type": "times",
                "old_stop": _stop_dict(oc),
                "new_stop": _stop_dict(nc),
                "arrival_delta_secs": arr_d,
                "departure_delta_secs": dep_d,
                "arrival_delta_minutes": round(arr_d / 60, 2),
                "departure_delta_minutes": round(dep_d / 60, 2),
            }
    raise AssertionError("两个班次完全一致，不存在变化")


def _calls_identical(old: TripView, new: TripView) -> bool:
    if old.signature != new.signature:
        return False
    return all(
        (oc.arrival_secs == nc.arrival_secs
         and oc.departure_secs == nc.departure_secs)
        for oc, nc in zip(old.stop_times, new.stop_times)
    )


def diff_snapshots(old: Snapshot, new: Snapshot, route_id: str,
                   date: str, weekday_col: str) -> dict:
    """比较两版快照，返回完整差异结果（纯函数，不做任何 I/O）。"""
    old_by_id = {t.trip_id: t for t in old.trips}
    new_by_id = {t.trip_id: t for t in new.trips}

    # ---- 1. trip_id 匹配（版本内 trip_id 唯一，天然一对一）----
    id_pairs: list[tuple[TripView, TripView]] = []
    for tid, ot in old_by_id.items():
        nt = new_by_id.get(tid)
        if nt is not None:
            id_pairs.append((ot, nt))
    matched_old = {t.trip_id for t, _ in id_pairs}
    matched_new = {t.trip_id for _, t in id_pairs}
    old_pool = [t for t in old.trips if t.trip_id not in matched_old]
    new_pool = [t for t in new.trips if t.trip_id not in matched_new]

    # ---- 2. 有序停站序列匹配 ----
    old_by_sig: dict[tuple[str, ...], list[TripView]] = {}
    new_by_sig: dict[tuple[str, ...], list[TripView]] = {}
    for t in old_pool:
        old_by_sig.setdefault(t.signature, []).append(t)
    for t in new_pool:
        new_by_sig.setdefault(t.signature, []).append(t)

    seq_pairs: list[tuple[TripView, TripView]] = []
    conflicts: list[dict] = []
    conflicted_old: set[str] = set()
    conflicted_new: set[str] = set()

    for sig in sorted(set(old_by_sig) | set(new_by_sig)):
        og = old_by_sig.get(sig, [])
        ng = new_by_sig.get(sig, [])
        if og and ng and (len(og) > 1 or len(ng) > 1):
            # 候选不唯一：报冲突，列出两侧对应班次，不擅自合并
            conflicts.append({
                "signature": list(sig),
                "old_trips": [_trip_dict(t) for t in og],
                "new_trips": [_trip_dict(t) for t in ng],
            })
            conflicted_old.update(t.trip_id for t in og)
            conflicted_new.update(t.trip_id for t in ng)
        elif og and ng:
            # 两侧各剩恰好 1 个同序列候选：匹配（id 匹配已先取走同 id 班次，
            # 不会破坏版本内 trip_id 一对一约束）
            seq_pairs.append((og[0], ng[0]))
        # 只剩一侧的情况留给新增 / 移除

    # ---- 3. 汇总新增 / 移除 ----
    paired_old = matched_old | conflicted_old | {t.trip_id for t, _ in seq_pairs}
    paired_new = matched_new | conflicted_new | {t.trip_id for _, t in seq_pairs}
    added = sorted((t for t in new.trips if t.trip_id not in paired_new),
                   key=lambda t: t.first_departure_secs)
    removed = sorted((t for t in old.trips if t.trip_id not in paired_old),
                     key=lambda t: t.first_departure_secs)

    # ---- 4. 匹配班次分类：未变化 / 调整 ----
    unchanged: list[dict] = []
    adjusted: list[dict] = []
    for matched_by, pairs in (("trip_id", id_pairs), ("stop_sequence", seq_pairs)):
        for ot, nt in sorted(pairs, key=lambda p: min(p[0].first_departure_secs,
                                                      p[1].first_departure_secs)):
            if _calls_identical(ot, nt):
                unchanged.append({
                    "matched_by": matched_by,
                    "old_trip_id": ot.trip_id,
                    "new_trip_id": nt.trip_id,
                })
            else:
                adjusted.append({
                    "matched_by": matched_by,
                    "old_trip": _trip_dict(ot),
                    "new_trip": _trip_dict(nt),
                    "first_change": _first_change(ot, nt),
                })

    return {
        "route_id": route_id,
        "date": date,
        "weekday": weekday_col,
        "old_version": {"id": old.version_id, "label": old.label,
                        "route_present": old.route is not None,
                        "route": old.route},
        "new_version": {"id": new.version_id, "label": new.label,
                        "route_present": new.route is not None,
                        "route": new.route},
        "summary": {
            "old_trip_count": len(old.trips),
            "new_trip_count": len(new.trips),
            "added": len(added),
            "removed": len(removed),
            "adjusted": len(adjusted),
            "unchanged": len(unchanged),
            "conflicts": len(conflicts),
        },
        "added": [_trip_dict(t) for t in added],
        "removed": [_trip_dict(t) for t in removed],
        "adjusted": adjusted,
        "unchanged": unchanged,
        "conflicts": conflicts,
    }
