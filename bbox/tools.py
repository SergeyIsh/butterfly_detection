#!/usr/bin/env python3
"""Prepare worker TSVs and convert Yandex Tasks TSV or adapted answer JSONL.

Python standard library only. Rectangles use normalized left/top/width/height.
All input is validated before publication; existing destinations are refused.
"""
import argparse
from collections import defaultdict
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit


BBOX = Path(__file__).resolve().parent
DATA = BBOX.parent / 'data'
INPUT = DATA / 'bbox_input_all_400.csv'
METADATA = DATA / 'metadata_all_400.csv'
FIELDS = {'photo_id', 'worker_id', 'assignment_id', 'status', 'boxes', 'comment'}
RECTANGLE = {'left', 'top', 'width', 'height'}


def photo_id(value):
    if type(value) not in (str, int) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise ValueError('photo_id must be a positive decimal ID without leading zeros')
    return str(value)


def read_csv(path, required):
    """Validate one snapshot, also used to hash exactly the parsed bytes."""
    data = path.read_bytes()
    reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig'), newline=''), strict=True)
    headers = reader.fieldnames or []
    if not required <= set(headers) or len(headers) != len(set(headers)):
        raise ValueError(f'{path}: missing or duplicate CSV headers; require {sorted(required)}')

    rows = {}
    for line, row in enumerate(reader, 2):
        if (
            None in row or any(value is None for value in row.values())
            or any(not row[key].strip() for key in required)
        ):
            raise ValueError(f'{path}:{line}: missing fields or malformed row')
        identifier = photo_id(row['photo_id'])
        if identifier in rows:
            raise ValueError(f'{path}:{line}: duplicate photo_id {identifier}')
        rows[identifier] = row
    if not rows:
        raise ValueError(f'{path}: no records')

    return rows, {'file': path.name, 'sha256': hashlib.sha256(data).hexdigest()}


def canonical_input(path):
    rows, fingerprint = read_csv(path, {'photo_id', 'image_url'})
    for identifier, row in rows.items():
        url = row['image_url']
        parsed = urlsplit(url)
        if (
            parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or any(char.isspace() for char in url)
        ):
            raise ValueError(f'photo_id {identifier}: image_url must be a valid HTTPS URL')
        _ = parsed.port  # Validate malformed ports too; no network requests are made.
    return rows, fingerprint


def rank(identifier):
    return hashlib.sha256(('bbox-pilot-v1:' + identifier).encode('ascii')).hexdigest()


def prepare(args):
    rows, input_hash = canonical_input(args.input)
    metadata, metadata_hash = read_csv(
        args.metadata, {'photo_id', 'category', 'original_url', 'license_code'}
    )
    if set(rows) != set(metadata):
        raise ValueError('input and metadata must contain exactly the same photo IDs')

    categories = defaultdict(list)
    for identifier, row in rows.items():
        meta = metadata[identifier]
        if meta['original_url'] != row['image_url']:
            raise ValueError(f'photo_id {identifier}: image_url differs from metadata original_url')
        if meta['license_code'] != 'cc0':
            raise ValueError(f'photo_id {identifier}: license_code must be cc0')
        categories[meta['category']].append(identifier)
    if len(rows) < 20 or 2 * len(categories) > 20 or any(len(ids) < 2 for ids in categories.values()):
        raise ValueError(
            '20-image pilot needs at least 20 images, at most 10 categories and 2 images per category'
        )

    selected = set()
    for ids in categories.values():
        selected.update(sorted(ids, key=rank)[:2])
    selected.update(sorted(set(rows) - selected, key=rank)[:20 - len(selected)])
    pilot = sorted(selected, key=rank)
    main = sorted(set(rows) - selected, key=rank)
    manifest = {
        'version': 1,
        'input': input_hash,
        'metadata': metadata_hash,
        'split_algorithm': 'SHA-256 bbox-pilot-v1:<photo_id>; 2/category then fill to 20',
        'counts': {'total': len(rows), 'pilot': len(pilot), 'main': len(main)},
        'pilot_photo_ids': pilot,
        'main_photo_ids': main,
    }

    out = args.out
    if out.exists() or out.is_symlink():
        raise ValueError(f'destination already exists: {out}; choose a new directory')
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bbox-prepare-', dir=out.parent) as temporary:
        staged = Path(temporary) / 'tasks'
        staged.mkdir()
        for name, ids in (('pilot', pilot), ('main', main)):
            with (staged / f'{name}.tsv').open('w', encoding='utf-8', newline='') as file:
                writer = csv.writer(file, delimiter='\t', lineterminator='\n')
                writer.writerow(['INPUT:photo_id', 'INPUT:image_url'])
                writer.writerows((identifier, rows[identifier]['image_url']) for identifier in ids)
        (staged / 'manifest.json').write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8'
        )
        out.mkdir()  # Exclusive reservation: never replace a pre-existing directory.
        try:
            os.replace(staged, out)  # Publish the complete directory atomically.
        except OSError:
            try:
                out.rmdir()
            except OSError:
                pass
            raise
    print(f'Prepared {len(pilot)} pilot + {len(main)} main images in {out}')


def reject_constant(value):
    raise ValueError(f'non-finite JSON number: {value}')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def validate_answer(answer, rows, seen):
    if not isinstance(answer, dict) or set(answer) != FIELDS:
        raise ValueError(f'adapted record must have exactly these fields: {sorted(FIELDS)}')
    identifier = photo_id(answer['photo_id'])
    if identifier not in rows:
        raise ValueError(f'unknown photo_id {identifier}')
    for name in ('worker_id', 'assignment_id'):
        if not isinstance(answer[name], str) or not answer[name].strip():
            raise ValueError(f'{name} must be a nonblank string')
    key = (answer['assignment_id'], identifier)
    if key in seen:
        raise ValueError(f'duplicate assignment_id/photo_id: {key}')

    status = answer['status']
    if not isinstance(status, str) or status not in {'objects', 'empty', 'load_error', 'uncertain'}:
        raise ValueError('status must be objects, empty, load_error or uncertain')
    boxes, comment = answer['boxes'], answer['comment']
    if not isinstance(boxes, list) or not isinstance(comment, str):
        raise ValueError('boxes must be a list and comment must be a string')
    if status == 'objects' and not boxes:
        raise ValueError('objects requires at least one rectangle')
    if status in {'empty', 'load_error'} and boxes:
        raise ValueError(f'{status} requires boxes=[]; remove stale rectangles explicitly')
    if status in {'load_error', 'uncertain'} and not comment.strip():
        raise ValueError(f'{status} requires a nonblank comment')

    result = []
    for box in boxes:
        if not isinstance(box, dict) or not RECTANGLE <= set(box) or set(box) - RECTANGLE - {'shape'}:
            raise ValueError('each rectangle needs left, top, width, height and optional shape=rectangle')
        if 'shape' in box and box['shape'] != 'rectangle':
            raise ValueError('only shape=rectangle is supported')
        box = {key: box[key] for key in ('left', 'top', 'width', 'height')}
        if any(type(value) not in (int, float) for value in box.values()):
            raise ValueError('rectangle coordinates must be numbers, not strings or booleans')
        try:
            finite = all(math.isfinite(value) for value in box.values())
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('rectangle coordinates must be finite')
        left, top, width, height = (box[key] for key in ('left', 'top', 'width', 'height'))
        if not (
            0 <= left < 1 and 0 <= top < 1 and 0 < width <= 1 and 0 < height <= 1
            and left + width <= 1 and top + height <= 1
        ):
            raise ValueError('rectangle must have positive size and fit inside normalized [0,1] image bounds')
        result.append(box)
    seen.add(key)
    return {
        'photo_id': identifier,
        'image_url': rows[identifier]['image_url'],
        'worker_id': answer['worker_id'],
        'assignment_id': answer['assignment_id'],
        'status': status,
        'boxes': result,
        'comment': comment,
    }


def decode_json(text):
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)


def read_answers(path, rows):
    with path.open(encoding='utf-8-sig', newline='') as file:
        if path.suffix.lower() != '.tsv':
            for number, line in enumerate(file, 1):
                try:
                    yield number, decode_json(line)
                except ValueError as e:
                    raise ValueError(f'{path}:{number}: {e}') from e
            return
        mapping = {
            'photo_id': 'INPUT:photo_id',
            'worker_id': 'ASSIGNMENT:worker_id',
            'assignment_id': 'ASSIGNMENT:assignment_id',
            'status': 'OUTPUT:status',
            'boxes': 'OUTPUT:boxes',
            'comment': 'OUTPUT:comment',
        }
        reader = csv.DictReader(file, delimiter='\t', strict=True)
        headers = reader.fieldnames or []
        if not set(mapping.values()) <= set(headers) or len(headers) != len(set(headers)):
            raise ValueError(
                f'{path}: native TSV requires unique headers including {sorted(mapping.values())}'
            )
        for row in reader:
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('malformed TSV row')
                answer = {key: row[column] for key, column in mapping.items()}
                identifier = photo_id(answer['photo_id'])
                if identifier not in rows:
                    raise ValueError(f'unknown photo_id {identifier}')
                if 'INPUT:image_url' in row and row['INPUT:image_url'] != rows[identifier]['image_url']:
                    raise ValueError('export image_url differs from canonical input')
                raw_boxes = answer['boxes']
                answer['boxes'] = decode_json(raw_boxes) if raw_boxes.strip() else []
                if answer['boxes'] is None:
                    answer['boxes'] = []
                # Cleared optional text can be exported as null by the platform.
                if answer['comment'] == 'null':
                    answer['comment'] = ''
                yield reader.line_num, answer
            except ValueError as e:
                raise ValueError(f'{path}:{reader.line_num}: {e}') from e


def convert(args):
    rows, _ = canonical_input(args.input)
    result, seen = [], set()
    for line_number, answer in read_answers(args.answers, rows):
        try:
            result.append(validate_answer(answer, rows, seen))
        except ValueError as e:
            raise ValueError(f'{args.answers}:{line_number}: {e}') from e
    if not result:
        raise ValueError('answer export is empty')

    out = args.out
    if out.exists() or out.is_symlink():
        raise ValueError(f'destination already exists: {out}; choose a new file')
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', prefix='.bbox-convert-', dir=out.parent
    ) as temporary:
        for answer in result:
            temporary.write(json.dumps(answer, ensure_ascii=False, allow_nan=False) + '\n')
        temporary.flush()
        os.fsync(temporary.fileno())
        os.link(temporary.name, out)  # Atomic publication which cannot overwrite.
    print(f'Validated {len(result)} responses in {out}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_parser = commands.add_parser(
        'prepare', help='validate CSVs and create a frozen 20-image pilot/main split'
    )
    prepare_parser.add_argument('--input', type=Path, default=INPUT)
    prepare_parser.add_argument('--metadata', type=Path, default=METADATA)
    prepare_parser.add_argument('--out', type=Path, default=BBOX / 'results/tasks')
    prepare_parser.set_defaults(run=prepare)

    convert_parser = commands.add_parser(
        'convert', help='convert native Yandex Tasks .tsv or adapted normalized JSONL'
    )
    convert_parser.add_argument('answers', type=Path, metavar='INPUT_TSV_OR_JSONL')
    convert_parser.add_argument(
        '--input', type=Path, default=INPUT, help='canonical CSV supplying photo IDs and URLs'
    )
    convert_parser.add_argument('--out', type=Path, required=True, metavar='OUTPUT_JSONL')
    convert_parser.set_defaults(run=convert)

    args = parser.parse_args()
    try:
        args.run(args)
    except (OSError, ValueError, csv.Error) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
