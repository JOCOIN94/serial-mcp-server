"""topology_engine.py — 토폴로지 상태 조정기(상태+Lock, Phase B 모듈6).

순수 모듈(events·correlator·routing·roster)을 한데 묶는다:
  리더 스레드 → observe(port, ts, text) → 포트별 EventAssembler 로 블록 조립 → 각 Event 를
  routing(링크그래프·토큰맵)·peerlinks(범용 포트쌍)·correlator(다중홉 상관)에 흘려
  홉을 방출하고 히스토리에 적재.
  sweep(now) → 유휴 pending 블록 flush + correlator 만료 처리(TX-without-RX).
  roster(entries) → routing 을 얹은 로스터 스냅샷. recent_hops(n) → 최근 홉(get_topology 용).
  recent_chains(n) → 메시지 단위 체인 로그. roster_and_recent_hops(entries,n,chains_n) →
  로스터 routing 스냅샷과 최근 홉/체인을 같은 Lock 세션에서 캡처.

리더 스레드(observe)·도구 호출(roster/recent_hops)·sweep 타이머가 공유 상태에 동시 접근하므로
단일 Lock 으로 보호한다(AGENTS.md 버퍼/공유상태 Lock). 윈도 클럭은 **서버 도착 단조시각 ts**
(server.py 가 time.monotonic 주입, 펌웨어 RTC 아님 — plan §6). 시리얼 I/O·타이머·부트스트랩
송신은 server.py 가 배선한다 — 이 클래스는 I/O·스레드 기동에 비의존이라 단위 테스트가 쉽다.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Optional

from .topology import build_roster
from .topology_chains import ChainLog
from .topology_correlator import Correlator
from .topology_events import EventAssembler
from .topology_pairing import CardPairing
from .topology_peerlinks import PeerLinks
from .topology_routing import RoutingTable


class _RoutingSnapshot:
    """build_roster 가 보는 routing 인터페이스(tokens()/edges()/info_table())의 불변 스냅샷.

    roster() 가 엔진 Lock 안에서 한 번만 떠서(RoutingTable 접근자들은 이미 사본 반환),
    CPU 무거운 build_roster(포트별 정규식 분류)는 Lock 밖에서 돌게 한다 — 뷰어 폴링이 리더
    스레드 observe 를 Lock 으로 막지 않도록(관측 비차단 불변식). edges 는 now 로 미리 계산됐다.
    """

    __slots__ = ("_tokens", "_edges", "_info")

    def __init__(self, tokens: dict, edges: list, info: Optional[dict] = None) -> None:
        self._tokens = tokens
        self._edges = edges
        self._info = info or {"by_mac": {}, "ssm_mac": {}}

    def tokens(self) -> dict:
        return self._tokens

    def edges(self, now=None) -> list:    # now 무시 — 스냅샷 시점 fresh 로 이미 계산됨
        return self._edges

    def info_table(self) -> dict:         # SSM INFO 테이블(mac↔UnID↔이름 다리 + 장비 RF)
        return self._info


class TopologyEngine:
    """관측 줄 → 홉/로스터. 순수 상태(Lock 보호), I/O 비의존."""

    def __init__(self, window_s: float = 15.0, hop_history: int = 200,
                 epoch_of=None) -> None:
        self._lock = threading.Lock()
        self._assemblers: dict[str, EventAssembler] = {}     # port → 누산기
        self._last_ts: dict[str, float] = {}                 # port → 최근 관측 ts(유휴 flush 판정)
        self._correlator = Correlator(window_s=window_s)
        self._routing = RoutingTable()
        self._hops: deque = deque(maxlen=hop_history)         # 최근 홉(상한·drop-oldest)
        # 메시지 단위 체인 로그. epoch_of=공개 ts 변환기(단조→epoch s, server.py 주입) —
        # 내부 윈도 클럭은 단조 유지, 뷰어/도구 공개 항목만 epoch 로 나간다.
        self._chains = ChainLog(window_s=window_s, epoch_of=epoch_of)
        self._chain_updates: dict[int, dict] = {}             # SSE 발행용 변경분(id→최신 사본)
        # SSM포트별 그룹 귀속 멤버십: {ssm_port: {unid: {device_type, local_port, last_ts}}}.
        # correlator 가 (UnID,Unique)+시간창으로 짝지은 rx-완료 홉(rx_port=SSM, src_port=leaf)에서 누적.
        self._membership: dict = {}
        self._peerlinks = PeerLinks(window_s=window_s)
        self._peer_scope_cache: dict = {}
        self._peer_scope_dirty = True
        self._port_idents_cache: dict = {}
        self._port_idents_dirty = True
        self._names_cache: dict = {}
        self._names_dirty = True              # membership 유래 무효화(라우팅 유래는 version 비교)
        self._names_routing_ver = -1
        # 카드상관 페어링: STM 은 베이번호를 안 흘려서, 카드 sCuID+UnID 로 포트→베이를 잇는다(build_roster 번호 폴백).
        self._pairing = CardPairing()

    def observe(self, port: str, ts: float, text: str) -> list:
        """리더 스레드가 수신 줄마다 호출(비차단·예외삼킴은 호출측 on_line 훅). 새 홉 리스트 반환."""
        with self._lock:
            asm = self._assemblers.get(port)
            if asm is None:
                asm = EventAssembler(port)
                self._assemblers[port] = asm
            self._last_ts[port] = ts
            self._pairing.observe(port, ts, text)     # 카드 sCuID 상관(포트→베이) 라이브 급전
            self._routing.observe_table_line(port, ts, text)   # SSM INFO 테이블 행(mac 다리) 급전
            return self._drain(asm.feed(ts, text), ts)

    def forget_port(self, port: str) -> None:
        """포트 disconnect/재오픈 시 그 포트의 카드페어링·멤버십 흔적 제거(스테일 매핑 방지). server.py 가 호출.

        멤버십도 함께 무효화한다 — 안 지우면 leaf 를 뽑거나 보드를 바꿔 꽂아도 SSM 이 무선으로
        계속 듣는 한 rx-only 홉이 last_ts 를 갱신하고 local_port 보존 규칙이 옛 포트를 유지해,
        존재하지 않는 포트로 fresh 링크선이 계속 그려진다(유령 링크). 재오픈 후 실제 매핑은
        다음 INFO 사이클의 TX↔RX 상관이 다시 채운다(관측 기반·자기 교정).
        """
        with self._lock:
            self._pairing.forget_port(port)
            self._peerlinks.forget_port(port)
            self._chains.forget_port(port)
            self._membership.pop(port, None)          # 이 포트가 SSM(rx_port)이던 그룹 멤버십 제거
            for members in self._membership.values():  # 이 포트를 leaf 로컬포트로 갖던 매핑 무효화
                for ent in members.values():
                    if ent.get("local_port") == port:
                        ent["local_port"] = None
            self._peer_scope_dirty = True
            self._port_idents_dirty = True
            self._names_dirty = True

    def sweep(self, now: float, flush_idle_s: float = 2.0) -> list:
        """유휴 pending 블록 flush + correlator 만료 처리. 새 홉 리스트 반환.

        flush_idle_s 이상 새 줄이 없던 포트의 마지막 블록을 방출한다(활성 블록은 절단 금지 —
        멀티라인 블록은 ms 단위 버스트라 수 초 유휴면 완결로 본다). 그 뒤 correlator.sweep 로
        윈도 만료 pending(TX-without-RX)을 실패/미확정으로 방출한다.
        """
        with self._lock:
            flushed: list = []
            for port, asm in list(self._assemblers.items()):
                if now - self._last_ts.get(port, now) >= flush_idle_s:
                    flushed += self._drain(asm.flush(), now)   # _drain 이 이미 _hops 에 적재함
            self._record_chain_updates(self._chains.sweep(now))
            swept = self._correlator.sweep(now)
            for hop in swept:
                self._record_chain_update(self._chains.apply_hop(hop, self._port_idents()))
            self._hops.extend(swept)                      # correlator.sweep 분만 추가 적재
            return flushed + swept

    def flush(self) -> list:
        """모든 포트의 pending 블록을 즉시 방출(종료/테스트). 새 홉 리스트 반환."""
        with self._lock:
            new: list = []
            for asm in self._assemblers.values():
                new += self._drain(asm.flush())
            return new

    def roster_and_recent_hops(self, entries, now: Optional[float] = None, n: int = 20,
                               chains_n: int = 0) -> tuple[dict, list, list]:
        """로스터용 routing 스냅샷과 최근 홉/체인을 한 Lock 세션에서 캡처해 반환한다.

        _drain 은 같은 Lock 안에서 routing/peerlinks/correlator 를 갱신하고 홉을 적재하므로,
        routing 스냅샷과 _hops tail 을 함께 뜨면 recent_hops 에만 있는 링크 skew 를 피할 수 있다.
        CPU 무거운 build_roster(포트별 정규식 분류)는 Lock 밖에서 수행해 관측 비차단을 유지한다.
        """
        with self._lock:
            snap = _RoutingSnapshot(self._routing.tokens(), self._routing.edges(now),
                                    self._routing.info_table())
            membership = self._membership_copy()
            pairing = self._pairing.snapshot()
            peer_links = self._peerlinks.snapshot(now)
            hops = [] if n <= 0 else list(self._hops)[-n:]
            chains = self._chains.recent(chains_n)
        return build_roster(entries, routing=snap, membership=membership, pairing=pairing,
                            peer_links=peer_links, now=now), hops, chains

    def roster(self, entries, now: Optional[float] = None) -> dict:
        """관측된 routing(링크그래프·토큰맵)을 얹은 로스터 스냅샷. 읽기 전용."""
        roster, _, _ = self.roster_and_recent_hops(entries, now=now, n=0, chains_n=0)
        return roster

    def recent_hops(self, n: int = 20) -> list:
        """최근 홉 n개(get_topology·SSE 보강용). 시간순 tail. n<=0 이면 빈 리스트."""
        with self._lock:
            if n <= 0:
                return []
            return list(self._hops)[-n:]

    def recent_chains(self, n: int = 30) -> list:
        """최근 체인 로그 n개(get_topology·뷰어 seed용). id 오름차순 tail."""
        with self._lock:
            return self._chains.recent(n)

    def drain_chain_updates(self) -> list:
        """관측 이후 변경된 체인 항목을 한 번만 반환한다(SSE 발행용)."""
        with self._lock:
            out = [self._chain_updates[k] for k in sorted(self._chain_updates)]
            self._chain_updates.clear()
            return out

    def membership_snapshot(self) -> dict:
        """SSM포트별 그룹 귀속 멤버십 사본 {ssm_port: {unid: {device_type, local_port, last_ts}}}.

        build_roster(모듈5)가 멀티-SSM 그룹 배치·leaf 로컬포트 연결에 쓴다. roster 스냅샷과 함께
        같은 Lock 세션에서 떠야 skew 가 없다(roster_and_recent_hops 가 그 경로). 읽기 전용 사본.
        """
        with self._lock:
            return self._membership_copy()

    def _membership_copy(self) -> dict:
        return {ssm: {unid: dict(ent) for unid, ent in members.items()}
                for ssm, members in self._membership.items()}

    def _drain(self, events, ts=None) -> list:
        """방출된 Event 들을 routing·peerlinks·correlator 에 흘려 홉을 모은다(Lock 보유 중 호출).

        observe/flush/sweep(유휴 flush) 모두 이 경로로 홉을 적재한다 — _drain 이 self._hops 에
        직접 extend 하므로(routing/peerlinks 는 부수상태 갱신, 방출 없음), 호출측은 반환값만 쓰고
        _hops 에 다시 넣지 않는다(이중 적재 주의). correlator.sweep 산출분은 _drain 을 안 거치므로
        sweep 이 그 분만 따로 _hops 에 적재한다. rx-완료 홉은 멤버십(SSM포트→leaf)에도 누적한다.
        """
        out: list = []
        for ev in events:
            self._routing.observe(ev)
            scope = self._peer_scope()
            names = self._names()
            port_idents = self._port_idents()
            self._record_chain_updates(self._chains.observe(ev, scope, resolver=self._routing,
                                                            port_names=names,
                                                            port_idents=port_idents))
            self._peerlinks.observe(ev, scope)
            out += self._correlator.observe(ev)
        for hop in out:
            self._record_membership(hop, ts)                   # 홉이 membership 을 먼저 갱신하고
            self._record_chain_update(self._chains.apply_hop(hop, self._port_idents()))  # 그 최신 ident 매핑으로 접목
        self._hops.extend(out)
        return out

    def _record_membership(self, hop: dict, ts) -> None:
        """rx-완료 홉(rx_port=SSM 수신 포트)으로 멤버십을 갱신. tx-only(sweep) 홉은 rx_port 없어 건너뜀.

        같은 (ssm_port, unid)에 후속 홉이 와도 새 홉에 없는 필드(예: rx-only 라 src_port=None)는
        기존 값을 보존한다 — leaf 로컬포트가 한 번 확인되면 유지(가장 최근 관측이 우선, 없으면 보존).
        """
        ssm_port = hop.get("rx_port")
        key = hop.get("key")
        if ssm_port is None or not key:
            return
        unid = key[0]
        members = self._membership.setdefault(ssm_port, {})
        prev = members.get(unid, {})
        members[unid] = {
            "device_type": hop.get("device_type") or prev.get("device_type"),
            "local_port": hop.get("src_port") or prev.get("local_port"),
            "last_ts": ts if ts is not None else prev.get("last_ts"),
            "rssi": hop.get("rssi") if hop.get("rssi") is not None else prev.get("rssi"),
        }
        self._peer_scope_dirty = True
        self._port_idents_dirty = True
        self._names_dirty = True

    def _record_chain_updates(self, updates: list) -> None:
        for ent in updates or []:
            self._record_chain_update(ent)

    def _record_chain_update(self, ent: Optional[dict]) -> None:
        if ent:
            self._chain_updates[ent["id"]] = ent

    def _names(self) -> dict:
        """ChainLog 노드 표기용 {port:name} 캐시.

        무효화 원천 2개: membership 변경(_names_dirty — _record_membership/forget_port)과
        라우팅 이름 원천 변경(RoutingTable.version() — 토큰맵·INFO 테이블·ssm_mac).
        둘 다 불변이면 재빌드 없이 재사용한다(리더 스레드 Lock 구간에서 이벤트마다 사본 생성 방지).
        """
        routing_ver = self._routing.version()
        if not self._names_dirty and routing_ver == self._names_routing_ver:
            return self._names_cache
        names = {}
        info = self._routing.info_table()
        by_mac = info.get("by_mac", {})
        unid_to_name = {}
        for ent in self._routing.tokens().values():
            if ent.get("unid") is not None and ent.get("name"):
                unid_to_name[ent["unid"]] = ent["name"]
        for ent in by_mac.values():
            if ent.get("unid") is not None and ent.get("name"):
                unid_to_name[ent["unid"]] = ent["name"]
        for port, mac in (info.get("ssm_mac") or {}).items():
            ent = by_mac.get(mac) or {}
            if ent.get("name"):
                names[port] = ent["name"]
        for members in self._membership.values():
            for unid, ent in members.items():
                lp = ent.get("local_port")
                if lp and unid in unid_to_name:
                    names[lp] = unid_to_name[unid]
        self._names_cache = names
        self._names_dirty = False
        self._names_routing_ver = routing_ver
        return names

    def _peer_scope(self) -> dict:
        """PeerLinks 그룹 veto 용 {port:ssm_port} 캐시."""
        if not self._peer_scope_dirty:
            return self._peer_scope_cache
        scope = {}
        best: dict = {}
        for ssm_port, members in self._membership.items():
            scope[ssm_port] = ssm_port
            for ent in members.values():
                lp = ent.get("local_port")
                if not lp:
                    continue
                ts = ent.get("last_ts")
                cur = best.get(lp)
                if cur is None or (ts is not None and (cur[1] is None or ts >= cur[1])):
                    best[lp] = (ssm_port, ts)
        for lp, (ssm_port, _ts) in best.items():
            scope[lp] = ssm_port
        self._peer_scope_cache = scope
        self._peer_scope_dirty = False
        return scope

    def _port_idents(self) -> dict:
        """ChainLog 자기 에코 판정용 {local_port: unid|mac} 캐시."""
        if not self._port_idents_dirty:
            return self._port_idents_cache
        best: dict = {}
        for members in self._membership.values():
            for ident, ent in members.items():
                lp = ent.get("local_port")
                if not lp:
                    continue
                ts = ent.get("last_ts")
                cur = best.get(lp)
                if cur is None or (ts is not None and (cur[1] is None or ts >= cur[1])):
                    best[lp] = (ident, ts)
        self._port_idents_cache = {lp: ident for lp, (ident, _ts) in best.items()}
        self._port_idents_dirty = False
        return self._port_idents_cache
