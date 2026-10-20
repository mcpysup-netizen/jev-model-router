"""Cached benchmark collection; network runs only in the refresh worker."""
from __future__ import annotations
import concurrent.futures
import csv
import datetime as dt
import fcntl
import io
import html
import threading
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.path.expanduser(os.environ.get('JEV_DATA_DIR', '~/.local/share/jev-model-router')))
CACHE = DATA_DIR / 'model-benchmarks.json'
TABLE = DATA_DIR / 'model-scorecard.md'
LIVE_ROOT = 'https://raw.githubusercontent.com/LiveBench/new-livebench/main/'
LIVE_INDEX = 'https://api.github.com/repos/LiveBench/new-livebench/contents/public'
KINDS = {'Coding': ['code', 'review'], 'Agentic Coding': ['code'], 'Reasoning': ['research', 'architecture'],
         'Language': ['writing'], 'IF': ['mechanical', 'scan'], 'Data Analysis': ['research']}
LEVELS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def read_cache(path=CACHE):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema_version') == 1 and isinstance(data.get('models'), dict):
            return data
    except (OSError, ValueError):
        pass
    return {'schema_version': 1, 'models': {}}


def due(data, model_ids, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if set(model_ids) - set(data.get('catalog_models', [])):
        return True
    try:
        fetched = dt.datetime.fromisoformat(data['refreshed_at'])
        return not dt.timedelta(0) <= now - fetched < dt.timedelta(days=7)
    except (KeyError, ValueError, TypeError):
        return True


def schedule_refresh(model_ids):
    data = read_cache()
    if not due(data, model_ids):
        return
    # Backoff only for repeated failure of the same catalog. A new model bypasses it.
    if set(model_ids) == set(data.get('attempted_models', [])):
        try:
            if dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(data['attempted_at']) < dt.timedelta(hours=1):
                return
        except (KeyError, ValueError, TypeError):
            pass
    try:
        worker = subprocess.Popen([sys.executable, str(HERE / 'jev_model_router.py'),
                          'refresh-benchmarks', '--benchmark-models', *model_ids],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        threading.Thread(target=worker.wait, daemon=True).start()
    except OSError as exc:
        print(f'benchmark refresh warning: {type(exc).__name__}; cached values retained', file=sys.stderr)


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Jev-Benchmarks/1.0'})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read(8_000_000).decode('utf-8')


def parse_livebench(text, categories, release, source, collected_at):
    models = {}
    for row in csv.DictReader(io.StringIO(text)):
        label = row.get('model', '')
        match = re.fullmatch(r'((?:gpt|claude)-[a-z0-9.\-]+?)-(low|medium|high|xhigh|max|ultra)(?:-effort)?', label)
        if not match:
            continue
        model, effort = match.groups()
        benchmarks = []
        for category, columns in categories.items():
            try:
                values = [float(row[column]) for column in columns]
            except (KeyError, ValueError, TypeError):
                continue
            if not values or not all(0 <= value <= 100 for value in values):
                continue
            benchmarks.append({'name': f'LiveBench {release} {category}', 'score': sum(values) / len(values),
                'unit': '%', 'effort': effort, 'kinds': KINDS.get(category, []), 'source': source,
                'source_type': 'independent', 'collected_at': collected_at, 'source_model_id': label,
                'comparison_group': f'livebench-{release}-{category}', 'comparable': True,
                'conditions': 'Same release, category subtask arithmetic mean; exact model and effort only',
                'subtasks': dict(zip(columns, values))})
        models.setdefault(model, {'benchmarks': []})['benchmarks'].extend(benchmarks)
    if not models:
        raise ValueError('LiveBench returned no recognized model-effort measurements')
    return models


def official_sources(model_id, previous):
    urls = []
    if model_id.startswith('gpt-'):
        urls.append(f'https://developers.openai.com/api/docs/models/{model_id}.md')
    else:
        urls.append('https://platform.claude.com/docs/en/models/overview.md')
    urls.extend(b['source'] for b in previous.get('benchmarks', []) if b.get('source_type') == 'official')
    # Announcement discovery comes from the current model page, not a fixed score list.
    return list(dict.fromkeys(urls))


def parse_official(text, model_id, source, stamp):
    # Accept machine-readable benchmark records or explicit Markdown tables.
    # Ambiguous prose/image tables are deliberately not guessed.
    values = []
    def plain(fragment):
        return html.unescape(re.sub('<[^>]+>', ' ', fragment)).strip()
    for table_html in re.findall(r'<table\b[^>]*>(.*?)</table>', text, re.S):
        column = None
        for row_html in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table_html, re.S):
            headers = re.findall(r'<th\b[^>]*>(.*?)</th>', row_html, re.S)
            cells = re.findall(r'<td\b[^>]*>(.*?)</td>', row_html, re.S)
            if any(re.search(r"(?:Opus|Sonnet|Fable|GPT).*\d", plain(h)) for h in headers):
                for index, header_html in enumerate(headers):
                    label = re.sub('[^a-z0-9]', '', plain(header_html).lower())
                    target = re.sub('[^a-z0-9]', '', model_id.lower()).removeprefix('claude')
                    if label in {target, 'claude' + target}:
                        column = index
                continue
            if column is None or column < 0 or column >= len(cells) or not headers:
                continue
            label_match = re.search(r'class="[^" ]*benchmark[^" ]*"[^>]*>(.*?)</span>', headers[0], re.S)
            name = plain(label_match[1] if label_match else headers[0]).rstrip('¹²³⁴⁵')
            score_match = re.search(r'(\d+(?:\.\d+)?)%', plain(cells[column]))
            if not score_match:
                continue
            score = float(score_match[1])
            if not 0 <= score <= 100:
                continue
            name += ' with tools' if 'with tools' in plain(cells[column]) else ''
            kinds = ['code', 'review'] if re.search('code|SWE|Terminal-Bench(?!-Science)', name, re.I) else ['mechanical'] if 'OSWorld' in name else ['research']
            values.append({'name': name, 'score': score, 'effort': None, 'unit': '%', 'kinds': kinds,
                'source': source, 'source_type': 'official', 'collected_at': stamp,
                'conditions': 'Official HTML table; footnote harness/effort not machine-verified',
                'comparison_group': source + ':' + name, 'comparable': False})
    # Handle real vendor matrix tables with model columns and benchmark rows.
    normalized_id = re.sub(r"[^a-z0-9]", "", model_id.lower())
    header, model_column = None, None
    for line in text.splitlines():
        if not line.startswith('|'):
            continue
        cells = [re.sub(r"[\[*`]", "", c.strip()) for c in line.strip('|').split('|')]
        for index, cell in enumerate(cells):
            normalized = re.sub(r"[^a-z0-9]", "", cell.lower())
            if normalized == normalized_id:
                header, model_column = cells, index
        if model_column is None or len(cells) != len(header):
            continue
        name = cells[0]
        if not re.search(r"bench|FrontierCode|OSWorld|Humanity|GPQA|AIME|MMLU", name, re.I):
            continue
        score_match = re.fullmatch(r"(\d+(?:\.\d+)?)%?", cells[model_column])
        if score_match:
            score = float(score_match[1])
            if 0 <= score <= 100:
                values.append({'name': name, 'score': score, 'unit': '%', 'effort': None,
                    'kinds': ['code', 'review'] if re.search(r"code|SWE|Terminal", name, re.I) else ['research'],
                    'source': source, 'source_type': 'official', 'collected_at': stamp,
                    'conditions': 'Vendor table, effort and harness not established',
                    'comparison_group': source + ':' + name, 'comparable': False})
    for line in text.splitlines():
        if not line.startswith('|') or model_id not in line:
            continue
        cells = [c.strip().strip('`') for c in line.strip('|').split('|')]
        # Stable explicit schema: model | benchmark | score | effort | conditions.
        if len(cells) != 5 or cells[0] != model_id or cells[3] not in LEVELS:
            continue
        try:
            score = float(cells[2].rstrip('%'))
        except ValueError:
            continue
        if not 0 <= score <= 100:
            continue
        name, conditions = cells[1], cells[4]
        kinds = ['code', 'review'] if re.search('code|SWE|Terminal', name, re.I) else ['research']
        values.append({'name': name, 'score': score, 'effort': cells[3], 'unit': '%', 'kinds': kinds,
                       'source': source, 'source_type': 'official', 'collected_at': stamp,
                       'conditions': conditions, 'comparison_group': name + ':' + conditions,
                       'comparable': bool(conditions)})
    return values


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def generate_table(data, path=TABLE):
    frontmatter = path.read_text(encoding='utf-8').split('---', 2)[1] if path.exists() else '''
description: "Jev 모델 성능 캐시에서 생성한 표"
type: reference
domain: engineering
use_when: "Jev 모델 선택 근거 확인"
load_priority: on-demand
tags: [Jev, 모델라우팅, 벤치마크]
version: 2
last_reviewed: 2026-10-03
source: model-benchmarks.json
'''
    frontmatter = re.sub(r'(?m)^source:.*$', 'source: "model-benchmarks.json"', frontmatter)
    frontmatter = re.sub(r'(?m)^version:.*$', 'version: 2', frontmatter)
    lines = ['---' + frontmatter + '---', '', '# 모델 성능표', '',
        '**자동 생성, 손편집 금지.** 정본은 `model-benchmarks.json`, 생성 명령은 `python3 jev_model_router.py refresh-benchmarks`다.', '',
        '자료 없음은 낮은 성능을 뜻하지 않는다. 모든 허용 후보에 같은 시험·버전·조건·추론 수준이 있을 때만 점수 순위를 사용한다. 내부 결과 5건 미만이고 공통 비교가 없으면 기존 역량 등급 규칙을 적용한다.', '',
        '추론 선택은 같은 모델·시험의 지원 수준 중 최고점에서 1점 이내인 가장 낮은 수준을 사용한다. 기존 기본 수준의 측정치가 없으면 기본 수준을 유지한다. high 위험 하한을 유지한다. LiveBench 값은 공개 하위 시험 점수의 산술 평균이며 실제 작업 통과율이 아니다.', '',
        f"최근 수집 시도: {data.get('attempted_at', '미확인')}, 최근 성공: {data.get('refreshed_at', '미확인')}", '',
        '| 모델 | 벤치마크 | 점수 | 추론 수준 | 수집 시각 | 출처 |', '|---|---|---|---|---|---|']
    sources = set()
    for model_id, model in sorted(data['models'].items()):
        for b in model.get('benchmarks', []):
            sources.add(b['source'])
            lines.append(f"| {model_id} | {b['name']} | {b['score']:.3f}% | {b.get('effort') or '미확인'} | {b.get('collected_at') or '미확인'} | [{b.get('source_type', 'official')}]({b['source']}) |")
        if not model.get('benchmarks'):
            lines.append(f'| {model_id} | 미확인 | 미확인 | 미확인 | 미확인 | |')
    lines += ['', '## 사양 참고', '', '가격·컨텍스트·지연은 캐시에 수집 시각과 함께 보관한 공급사 사양이다. 벤치마크 순위에 합산하지 않는다.', '',
              '| 모델 | 입력/출력 USD, 100만 토큰 | 컨텍스트 | 지연 | 출처 |', '|---|---|---|---|---|']
    for model_id, model in sorted(data['models'].items()):
        if model.get('metadata_source'):
            url = model['metadata_source']
            sources.add(url)
            lines.append(f"| {model_id} | {model.get('input_usd_per_million', '미확인')} / {model.get('output_usd_per_million', '미확인')} | {model.get('context_tokens', '미확인')} | {model.get('latency', '미확인')} | [사양]({url}) |")
    lines += ['', '## 수집 경고', ''] + ['- ' + w for w in data.get('warnings', [])]
    lines += ['', '## 이용 조건', '', data.get('usage_terms', '미확인'), '', '## 근거·출처', '']
    lines += [f'- [측정 출처]({url})' for url in sorted(sources)]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def refresh_benchmarks(model_ids=None, force=False, path=CACHE, table=TABLE):
    lock = path.with_suffix('.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'already_running'}
        data = read_cache(path)
        if model_ids is None:
            import jev_model_router as router
            visible, _ = router.codex_catalog(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'models_cache.json')
            claude, _ = router.claude_catalog(Path.home() / '.cache/jev-model-router/claude_models.json')
            model_ids = [m['slug'] for m in router.latest_codex_models(visible)] + [m['api_model_id'] for m in claude.values()]
        if not force and not due(data, model_ids):
            generate_table(data, table)
            return {'status': 'fresh_cache', 'models': len(data['models'])}
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        warnings, fresh, successful_sources = [], {}, []
        try:
            robots = fetch('https://livebench.ai/robots.txt')
            if re.search(r'Disallow:\s*/\s*$', robots, re.M):
                raise ValueError('LiveBench robots restricts collection')
            entries = json.loads(fetch(LIVE_INDEX))
            filename = max(e['name'] for e in entries if re.fullmatch(r'table_\d{4}_\d{2}_\d{2}\.csv', e['name']))
            release = filename[6:-4]
            source = LIVE_ROOT + 'public/' + filename
            categories = json.loads(fetch(LIVE_ROOT + f'public/categories_{release}.json'))
            fresh = parse_livebench(fetch(source), categories, release, source, stamp)
            successful_sources.append(source)
            data['usage_terms'] = ('[LiveBench robots](https://livebench.ai/robots.txt)는 Disallow 없음. '
                '[공개 데이터 구조](https://github.com/LiveBench/new-livebench/blob/main/README.md)를 확인했다. '
                'new-livebench에는 별도 LICENSE가 없으므로 원 평가 코드의 Apache-2.0을 데이터 재배포 허가로 간주하지 않는다. '
                '공개 수치를 출처와 함께 로컬 캐시에 보관하며 원 CSV·코드를 재배포하지 않는다. 공개 재배포 권한은 미확인이다.')
        except Exception as exc:
            warnings.append(f'LiveBench: {type(exc).__name__}: {exc}; 이전 값 유지')
        jobs = [(model, url) for model in model_ids for url in official_sources(model, data['models'].get(model, {}))]
        def collect(job):
            model, url = job
            try:
                text = fetch(url)
                scores = parse_official(text, model, url, stamp)
                linked = re.findall(r'https://(?:www\.)?(?:openai\.com/index|anthropic\.com/(?:news|claude))[^\s)"<>]+', text)
                for link in dict.fromkeys(linked):
                    try:
                        scores += parse_official(fetch(link), model, link, stamp)
                    except Exception:
                        pass
                return model, url, scores, None
            except Exception as exc:
                return model, url, [], f'{model} {url}: {type(exc).__name__}; 이전 값 유지'
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            for model, url, scores, error in executor.map(collect, jobs):
                if error:
                    warnings.append(error)
                else:
                    data['models'].setdefault(model, {'benchmarks': []})['metadata_collected_at'] = stamp
                    if url == official_sources(model, {})[0]:
                        data['models'][model]['metadata_source'] = url
                    successful_sources.append(url)
                    if not scores:
                        warnings.append(f'{model} {url}: 명시적 모델·점수·추론 조건 수치 미확인, 이전 값 유지')
                    fresh.setdefault(model, {'benchmarks': []})['benchmarks'].extend(scores)
        for values in fresh.values():
            unique = {}
            for measurement in values['benchmarks']:
                key = (measurement['source'], measurement['name'], measurement.get('effort'), measurement['score'])
                unique[key] = measurement
            values['benchmarks'] = list(unique.values())
        received_count = sum(len(m['benchmarks']) for m in fresh.values())
        for model, values in fresh.items():
            old = data['models'].setdefault(model, {'benchmarks': []})
            replaced = {b['source'] for b in values['benchmarks']}
            independent = any(b['source_type'] == 'independent' for b in values['benchmarks'])
            old['benchmarks'] = [b for b in old.get('benchmarks', []) if b.get('source') not in replaced and not (independent and b.get('source_type') == 'independent')] + values['benchmarks']
        data.update(attempted_at=stamp, attempted_models=sorted(model_ids), warnings=warnings)
        if received_count:
            data.update(refreshed_at=stamp, catalog_models=sorted(model_ids))
        result = {'status': 'refreshed' if received_count else 'retained_cache', 'received_models': sum(bool(v['benchmarks']) for v in fresh.values()),
                  'received_items': received_count, 'catalog_models': len(model_ids), 'sources_succeeded': len(successful_sources), 'warnings': warnings}
        data['last_refresh_result'] = result
        atomic_json(path, data)
        generate_table(data, table)
        for warning in warnings:
            print('benchmark refresh warning: ' + warning, file=sys.stderr)
        return result
