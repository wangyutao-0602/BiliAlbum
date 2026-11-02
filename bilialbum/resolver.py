"""Discover multi-P videos and paginated UGC seasons/series.

The view endpoint identifies the parent collection; the paginated endpoint
must complete before an 'all' operation is offered. Never download a partial
season silently.
"""
from __future__ import annotations
from dataclasses import replace
import re
from .api import BiliAPI
from .models import Collection, Episode, BiliAlbumError, NetworkFailure
from .metadata import extract_view
from .urltools import normalize_url, ParsedURL


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None
    if isinstance(v, str) and not re.fullmatch(r'[0-9]{1,20}', v):
        return None
    return int(v) if int(v) > 0 else None


def _total(expected, *values):
    for value in values:
        if value is None:
            continue
        number = 0 if type(value) in (int, str) and value in (0, '0') else _num(value)
        if number is None:
            raise BiliAlbumError('合集分页总数无效，无法证明成员完整。')
        if expected is not None and expected != number:
            raise BiliAlbumError('合集分页总数发生变化，请稍后重新解析，已阻止不完整下载。')
        expected = number
    return expected


def _append_records(records, batch, seen):
    if not isinstance(batch, list):
        raise BiliAlbumError('合集分页结构异常。')
    for record in batch:
        ep = _video_record(record, 0)
        if ep.bvid in seen:
            raise BiliAlbumError('合集数据存在重复的 BV 号，无法证明集数完整。')
        seen.add(ep.bvid)
        records.append(record)


def _sort_chronological(episodes: list[Episode]) -> tuple[list[Episode], str]:
    if episodes and all(x.published is not None for x in episodes):
        order = sorted(enumerate(episodes), key=lambda pair: (pair[1].published, pair[0]))
        return [replace(ep, number=i) for i, (_, ep) in enumerate(order, 1)], '发布时间升序（同时间保持平台原顺序）'
    return [replace(ep, number=i) for i, ep in enumerate(episodes, 1)], '部分发布日期缺失：保留平台原顺序（不伪造时间）'


def _video_record(record: dict, n: int) -> Episode:
    if not isinstance(record, dict):
        raise BiliAlbumError('合集成员格式异常。')
    arc = record.get('arc') or {}
    if not isinstance(arc, dict):
        arc = {}
    bvid = record.get('bvid') or arc.get('bvid')
    if not isinstance(bvid, str) or not re.fullmatch(r'BV[A-Za-z0-9]{10}', bvid):
        raise BiliAlbumError('合集包含无法识别 BV 号的条目，拒绝执行可能错误的全集下载。')
    return Episode(n, str(record.get('title') or arc.get('title') or bvid), bvid,
                   published=_num(record.get('pubdate') or record.get('ptime') or arc.get('pubdate')),
                   # Archive durations may cover all P of a BV; they cannot
                   # validate the P1 media duration. The selected view overrides it.
                   duration=None,
                   cid=_num(record.get('cid') or arc.get('cid')))


class Resolver:
    def __init__(self, api: BiliAPI | None = None, *, direct=False):
        self.prefer_public_page = api is None
        self.api = api or BiliAPI(direct=direct)
        self.direct = direct or getattr(self.api, 'direct', False)

    def resolve(self, user_url: str) -> Collection:
        url = normalize_url(user_url, direct=self.direct)
        if url.video_id:
            query = {'bvid': url.video_id} if url.video_id.startswith('BV') else {'aid': url.video_id[2:]}
            initial_info = None
            if self.prefer_public_page:
                # The public page is sufficient for BV/av metadata and DASH.
                # Do not make it depend on a separate legacy API being reachable.
                data, initial_info = extract_view(url, direct=self.direct)
            else:
                try:
                    data = self.api.get('/x/web-interface/view', query)
                except NetworkFailure as exc:
                    if exc.status != 412 and 'HTTP 412' not in exc.stage:
                        raise
                    data, initial_info = extract_view(url, direct=self.direct)
            if not isinstance(data, dict) or not data.get('bvid'):
                raise BiliAlbumError('无法获取此视频的有效元数据。')
            result = self._from_view(data, url)
            result.initial_info = initial_info
            return result
        if url.list_type == 'season':
            return self._season(url.mid or '', url.list_id or '', url.url, None)
        if url.list_type == 'series':
            return self._series(url.mid or '', url.list_id or '', url.url)
        raise BiliAlbumError('暂不支持此类链接。')

    def _from_view(self, data: dict, url: ParsedURL) -> Collection:
        bvid = data['bvid']
        if not isinstance(bvid, str) or not re.fullmatch(r'BV[A-Za-z0-9]{10}', bvid):
            raise BiliAlbumError('视频元数据包含无效的 BV 号。')
        pages = data.get('pages')
        p = url.page
        if pages is None or pages == []:
            pages = [{'page': 1, 'part': data.get('title'), 'cid': data.get('cid'), 'duration': data.get('duration')}]
        if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
            raise BiliAlbumError('分 P 数据格式异常。')
        page_numbers = [_num(page.get('page', i)) for i, page in enumerate(pages, 1)]
        if None in page_numbers or len(set(page_numbers)) != len(page_numbers):
            raise BiliAlbumError('分 P 编号重复或无效，已拒绝自动改为 P1。')
        if p not in page_numbers:
            raise BiliAlbumError(f'找不到网址要求 P{p}，已拒绝自动改为 P1。')
        season = data.get('ugc_season') or {}
        if isinstance(season, dict) and season.get('id'):
            mid = ((data.get('owner') or {}).get('mid') or season.get('mid'))
            if mid:
                result = self._season(str(mid), str(season['id']), url.url, (bvid, p), season)
                selected_page = next(page for page, number in zip(pages, page_numbers) if number == p)
                result.episodes = [replace(ep, cid=_num(selected_page.get('cid')),
                                           duration=selected_page.get('duration'))
                                   if ep.bvid == bvid else ep for ep in result.episodes]
                return result
        if len(pages) > 1:
            entries = [Episode(number,
                               f'{data.get("title") or bvid} - {page.get("part") or f"P{i}"}',
                               bvid, page=number,
                               cid=_num(page.get('cid')),
                               published=_num(data.get('pubdate')),
                               duration=page.get('duration'))
                       for i, (page, number) in enumerate(zip(pages, page_numbers), 1)]
            entries.sort(key=lambda e: e.page)
            entries = [replace(ep, number=i) for i, ep in enumerate(entries, 1)]
            selected = next((ep.number for ep in entries if ep.page == p), None)
            if selected is None:
                raise BiliAlbumError(f'找不到网址指定的分 P{p}。')
            return Collection(str(data.get('title') or bvid), 'multip', entries, selected,
                              '同 BV 多 P：按原始 P 序号升序排列', url.url)
        return Collection(str(data.get('title') or bvid), 'single', [
            Episode(1, str(data.get('title') or bvid), bvid, page=p, cid=_num(pages[0].get('cid')),
                    published=_num(data.get('pubdate')), duration=pages[0].get('duration'))
        ], 1, '普通单视频', url.url)

    def _season(self, mid: str, season_id: str, source_url: str,
                anchor: tuple[str, int] | None, embedded: dict | None = None) -> Collection:
        records: list[dict] = []
        expected: int | None = None
        error: BiliAlbumError | None = None
        returned_title: str | None = None
        seen_records = set()
        for pn in range(1, 201):
            try:
                data = self.api.get('/x/polymer/web-space/seasons_archives_list', {
                    'mid': mid, 'season_id': season_id, 'sort_reverse': 'false',
                    'page_num': pn, 'page_size': 50,
                })
            except BiliAlbumError as exc:
                error = exc
                break
            batch = data.get('archives', [])
            if not isinstance(batch, list):
                raise BiliAlbumError('合集分页结构异常。')
            _append_records(records, batch, seen_records)
            page_meta = data.get('page') or {}
            season_meta = data.get('meta') or {}
            returned_title = returned_title or season_meta.get('name') or season_meta.get('title')
            expected = _total(expected, page_meta.get('total'), season_meta.get('total'))
            if not batch or (expected is not None and len(records) >= expected):
                break
        else:
            raise BiliAlbumError('合集分页超过安全上限，拒绝只下载部分内容。')

        title = (embedded or {}).get('title') or returned_title or f'合集 {season_id}'
        if not records and embedded:
            sections = embedded.get('sections') or []
            for section in sections:
                records.extend(section.get('episodes') or [])
            embedded_expected = _num(embedded.get('ep_count') or embedded.get('total'))
            if embedded_expected is None or len(records) != embedded_expected:
                raise BiliAlbumError('无法证明合集成员已全部读完（分页接口受限或返回部分视频），已阻止不完整下载。') from error
            if not records:
                raise BiliAlbumError('无法获取合集列表，请尝试单个分 P 链接或稍后重试。') from error
            # The embedded list was independently proven complete. Preserve that proof
            # for duplicate checks instead of treating the earlier API error as fatal.
            expected = embedded_expected
            error = None
        if error and records:
            raise BiliAlbumError('合集分页中途失败，拒绝下载不完整的合集，请稍后重试。') from error
        if not records:
            raise error or BiliAlbumError('该合集未返回任何可访问条目。')
        if expected is not None and len(records) != expected:
            raise BiliAlbumError(f'合集应有 {expected} 集，但只读到 {len(records)} 集，已阻止不完整下载。')
        entries = []
        seen: set[str] = set()
        for record in records:
            ep = _video_record(record, 0)
            if ep.bvid in seen:
                continue
            seen.add(ep.bvid)
            entries.append(ep)
        if expected is not None and len(entries) != expected:
            raise BiliAlbumError('合集数据存在重复或缺失的 BV 号，无法证明集数完整。')
        entries, note = _sort_chronological(entries)
        if anchor:
            current_bvid, current_page = anchor
            selected = next((ep.number for ep in entries if ep.bvid == current_bvid), None)
            if selected is None:
                raise BiliAlbumError('当前视频未出现在返回的合集成员中，拒绝误选其他集。')
            if current_page != 1:
                entries = [replace(ep, page=current_page, cid=None, duration=None)
                           if ep.bvid == current_bvid else ep for ep in entries]
                note += '；当前链接指向的 BV 保留其 P 参数，其余 BV 默认 P1'
        else:
            selected = 1
        return Collection(str(title), 'season', entries, selected, note, source_url)

    def _series(self, mid: str, series_id: str, source_url: str) -> Collection:
        records: list[dict] = []
        expected = None
        series_title: str | None = None
        seen_records = set()
        for pn in range(1, 201):
            data = self.api.get('/x/series/archives', {
                'mid': mid, 'series_id': series_id, 'only_normal': 'true',
                'sort': 'desc', 'pn': pn, 'ps': 50,
            })
            batch = data.get('archives', [])
            if not isinstance(batch, list):
                raise BiliAlbumError('系列分页格式异常。')
            _append_records(records, batch, seen_records)
            page = data.get('page') or {}
            series_meta = data.get('meta') or {}
            series_title = series_title or series_meta.get('name') or series_meta.get('title')
            expected = _total(expected, page.get('total'))
            if not batch or (expected is not None and len(records) >= expected):
                break
        else:
            raise BiliAlbumError('系列集数超过安全分页上限。')
        if expected is not None and len(records) != expected:
            raise BiliAlbumError('系列列表未完整取得，请稍后重试。')
        if not records:
            raise BiliAlbumError('无法读取该系列，可能已删除或不可访问。')
        entries, note = _sort_chronological([_video_record(r, i + 1) for i, r in enumerate(records)])
        return Collection(str(series_title or f'系列 {series_id}'), 'series', entries, 1, note, source_url)
