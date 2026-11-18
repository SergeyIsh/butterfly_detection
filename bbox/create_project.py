#!/usr/bin/env python3
"""Create a Yandex Tasks bbox project and closed pools; default is an offline plan."""
import argparse
import copy
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from tasks_api import ApiClient, ApiError, ProvisioningError, apply_plan

BBOX = Path(__file__).resolve().parent
PREFIXES = ('view.', 'field.', 'condition.', 'plugin.', 'layout.', 'helper.', 'action.')
STAGES = ('training', 'exam', 'pilot', 'main')


def check_integer(value, name, minimum=1, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'{name}: expected integer in {minimum}..{maximum or "∞"}')


def validate_settings(settings, now):
    keys = {
        'api_token', 'deployment_key', 'public_name', 'public_description', 'will_expire',
        'assignment_max_duration_seconds', 'auto_accept_period_day',
        'admission_score', 'training_admission_score', *STAGES
    }
    if not isinstance(settings, dict) or set(settings) != keys:
        raise ValueError(f'api.json must contain exactly: {sorted(keys)}')
    if not isinstance(settings['api_token'], str):
        raise ValueError('api_token: expected a string')
    key = settings['deployment_key']
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{2,60}', key):
        raise ValueError('deployment_key: use 3–61 ASCII letters, digits, underscores or hyphens')
    for name in ('public_name', 'public_description'):
        if not isinstance(settings[name], str) or not settings[name].strip():
            raise ValueError(f'{name}: expected nonblank text')
    try:
        expires = datetime.fromisoformat(settings['will_expire'].replace('Z', '+00:00'))
        if expires.tzinfo is None or expires <= now:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('will_expire: set a future ISO 8601 date with timezone') from None
    check_integer(settings['assignment_max_duration_seconds'], 'assignment_max_duration_seconds')
    check_integer(settings['auto_accept_period_day'], 'auto_accept_period_day', 1, 21)
    for name in ('admission_score', 'training_admission_score'):
        check_integer(settings[name], name, 0, 100)

    for role in STAGES:
        stage = settings[role]
        stage_keys = {'tasks_per_assignment', 'reward_per_assignment', 'overlap'}
        if isinstance(stage, dict) and role == 'main' and 'fast_answers' in stage:
            stage_keys.add('fast_answers')
        if not isinstance(stage, dict) or set(stage) != stage_keys:
            raise ValueError(f'{role}: set tasks_per_assignment, reward_per_assignment and overlap')
        check_integer(stage['tasks_per_assignment'], f'{role}.tasks_per_assignment', 1, 500)
        check_integer(stage['overlap'], f'{role}.overlap')
        reward = stage['reward_per_assignment']
        if type(reward) not in (int, float) or not math.isfinite(reward) or reward < 0.5:
            raise ValueError(f'{role}.reward_per_assignment: minimum 0.5 RUB per assignment')

    fast_answers = settings['main'].get('fast_answers')
    if fast_answers is not None:
        if not isinstance(fast_answers, dict) or set(fast_answers) != {'min_seconds', 'count', 'ban_days'}:
            raise ValueError('main.fast_answers: set min_seconds, count and ban_days, or use null')
        for name in ('min_seconds', 'count', 'ban_days'):
            check_integer(fast_answers[name], f'main.fast_answers.{name}')


def prepare_template(interface, lock):
    if not isinstance(interface, dict) or 'view' not in interface:
        raise ValueError('interface.json: expected a Template Builder config with view')
    if not isinstance(lock, dict) or 'core' not in lock:
        raise ValueError('interface.lock.json must include core')
    if any(
        not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
        for version in lock.values()
    ):
        raise ValueError('interface.lock.json: versions must have major.minor.patch format')
    result = copy.deepcopy(interface)

    def remove_versions(node):
        if isinstance(node, list):
            for value in node:
                remove_versions(value)
        elif isinstance(node, dict):
            component = node.get('type')
            if isinstance(component, str) and component.startswith(PREFIXES):
                if component not in lock:
                    raise ValueError(f'interface.lock.json is missing {component}')
                if 'version' in node and node.pop('version') != lock[component]:
                    raise ValueError(f'component version differs from lock: {component}')
            for value in node.values():
                remove_versions(value)

    remove_versions(result)
    return result


def instruction_html(template, examples_base_url):
    if not isinstance(template, str) or '{{EXAMPLES_BASE_URL}}' not in template:
        raise ValueError('HTML instructions must contain {{EXAMPLES_BASE_URL}}')
    if examples_base_url is None:
        examples = json.loads((BBOX / 'instructions/examples.json').read_text(encoding='utf-8'))['examples']
        for example in examples:
            source = '{{EXAMPLES_BASE_URL}}/' + Path(example['asset']).name
            pattern = r'<img\b[^>]*\bsrc="' + re.escape(source) + r'"[^>]*>'
            photo = (
                '<img src="' + html.escape(example['image_url'], quote=True)
                + '" alt="' + html.escape(example['title'], quote=True)
                + '" style="display:block;width:100%;height:auto;">'
            )
            rectangles = []
            for box in example['illustration_boxes']:
                position = ';'.join(
                    f'{key}:{box[key] * 100:.4f}%'
                    for key in ('left', 'top', 'width', 'height')
                )
                rectangles.append(
                    '<span style="position:absolute;display:block;' + position
                    + ';box-sizing:border-box;border:3px solid #00d68f;outline:1px solid #123b2e;pointer-events:none;"></span>'
                )
            rendered = (
                '<span style="position:relative;display:inline-block;width:100%;max-width:1024px;line-height:0;">'
                + photo + ''.join(rectangles) + '</span>'
            )
            template, count = re.subn(pattern, lambda match: rendered, template)
            if count != 1:
                raise ValueError('Instruction image must occur once: ' + source)
        return template
    url = urlsplit(examples_base_url)
    if (
        url.scheme != 'https' or not url.hostname or url.username is not None
        or url.password is not None or any(char.isspace() for char in examples_base_url)
        or '?' in examples_base_url or '#' in examples_base_url
    ):
        raise ValueError(
            'examples-base-url must be a public HTTPS directory URL without credentials, spaces, query or fragment'
        )
    _ = url.port
    return template.replace('{{EXAMPLES_BASE_URL}}', html.escape(examples_base_url.rstrip('/'), quote=True))


def build_plan(settings, interface, lock, instructions, examples_base_url=None, with_training=False, now=None):
    """Pure API payloads. References are resolved only after dependencies exist."""
    validate_settings(settings, now or datetime.now(timezone.utc))
    template = prepare_template(interface, lock)
    steps = []
    instructions = instruction_html(instructions, examples_base_url)

    def add(role, endpoint, payload):
        steps.append({'role': role, 'endpoint': endpoint, 'payload': payload})

    add('project', '/projects', {
        'public_name': settings['public_name'],
        'public_description': settings['public_description'],
        'public_instructions': instructions,
        'assignments_issuing_type': 'AUTOMATED',
        'assignments_automerge_enabled': False,
        'task_spec': {
            'input_spec': {
                'photo_id': {'type': 'string', 'required': True},
                'image_url': {'type': 'url', 'required': True}
            },
            'output_spec': {
                'status': {
                    'type': 'string',
                    'required': True,
                    'allowed_values': ['objects', 'empty', 'load_error', 'uncertain']
                },
                'boxes': {'type': 'json', 'required': False},
                'comment': {'type': 'string', 'required': False},
            },
            'view_spec': {
                'type': 'tb',
                'config': json.dumps(template, ensure_ascii=False, allow_nan=False),
                'lock': copy.deepcopy(lock),
                'inferDataSpec': False,
                'settings': {
                    'showInstructions': True,
                    'showSkip': True,
                    'showSubmit': True,
                    'showFullscreen': True
                }
            },
        },
    })
    add('exam_skill', '/skills', {'name': settings['deployment_key'] + '-exam', 'hidden': True})
    if with_training:
        add('training_skill', '/skills', {'name': settings['deployment_key'] + '-training', 'hidden': True})

    names = {
        'training': 'Обучение · ручная проверка',
        'exam': 'Экзамен · ручная проверка',
        'pilot': 'Пилот',
        'main': 'Основная разметка'
    }
    for role in (STAGES if with_training else STAGES[1:]):
        pool_config = settings[role]
        filters = [{
            'category': 'computed',
            'key': 'device_category',
            'operator': 'EQ',
            'value': 'PERSONAL_COMPUTER'
        }]
        if role in ('training', 'exam'):
            filters.append({'category': 'skill', 'key': '$ref:' + role + '_skill', 'operator': 'EQ'})
            if role == 'exam' and with_training:
                filters.append({
                    'category': 'skill',
                    'key': '$ref:training_skill',
                    'operator': 'GTE',
                    'value': settings['training_admission_score']
                })
        else:
            filters.append({
                'category': 'skill',
                'key': '$ref:exam_skill',
                'operator': 'GTE',
                'value': settings['admission_score']
            })
        pool = {
            'project_id': '$ref:project',
            'private_name': names[role],
            'will_expire': settings['will_expire'],
            'may_contain_adult_content': False,
            'reward_per_assignment': pool_config['reward_per_assignment'],
            'assignment_max_duration_seconds': settings['assignment_max_duration_seconds'],
            'auto_accept_solutions': False,
            'auto_accept_period_day': settings['auto_accept_period_day'],
            'defaults': {
                'default_overlap_for_new_tasks': pool_config['overlap'],
                'default_overlap_for_new_task_suites': pool_config['overlap']
            },
            'mixer_config': {
                'real_tasks_count': pool_config['tasks_per_assignment'],
                'golden_tasks_count': 0,
                'training_tasks_count': 0,
                'min_real_tasks_count': pool_config['tasks_per_assignment'],
                'force_last_assignment': False,
                'mix_tasks_in_creation_order': True,
                'shuffle_tasks_in_task_suite': True
            },
            'filter': {'and': [{'or': [condition]} for condition in filters]},
        }
        fast_answers = pool_config.get('fast_answers')
        if role == 'main' and fast_answers is not None:
            pool['quality_control'] = {'configs': [{
                'collector_config': {
                    'type': 'ASSIGNMENT_SUBMIT_TIME',
                    'parameters': {'fast_submit_threshold_seconds': fast_answers['min_seconds']}
                },
                'rules': [{
                    'conditions': [{
                        'key': 'fast_submitted_count',
                        'operator': 'GTE',
                        'value': fast_answers['count']
                    }],
                    'action': {
                        'type': 'RESTRICTION_V2',
                        'parameters': {
                            'scope': 'POOL',
                            'duration_unit': 'DAYS',
                            'duration': fast_answers['ban_days']
                        }
                    }
                }]
            }]}
        add(role, '/pools', pool)
    return steps


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=BBOX / 'configs/api.json')
    parser.add_argument('--interface', type=Path, default=BBOX / 'configs/interface.json')
    parser.add_argument('--lock', type=Path, default=BBOX / 'configs/interface.lock.json')
    parser.add_argument(
        '--instructions', type=Path, default=BBOX / 'instructions/annotator.html',
        help='Instruction file in HTML'
    )
    parser.add_argument(
        '--examples-base-url',
        help='Optional public folder with annotated JPEGs; default uses source photos with HTML rectangles'
    )
    parser.add_argument(
        '--with-training', action='store_true', help='Add paid practice training with manual skill grading'
    )
    parser.add_argument(
        '--apply', action='store_true', help='Create resources using api_token from config; never opens pools'
    )
    parser.add_argument('--auth-scheme', choices=('OAuth', 'ApiKey'), default='OAuth')
    parser.add_argument('--state', type=Path, default=BBOX / 'results/api_state.json')
    parser.add_argument(
        '--adopt', action='append', default=[], metavar='ROLE=ID',
        help='Resolve an uncertain earlier create; only with --apply'
    )
    args = parser.parse_args(argv)

    try:
        if args.adopt and not args.apply:
            raise ValueError('--adopt requires --apply and an existing pending resource')
        read_json = lambda path: json.loads(path.read_text(encoding='utf-8'))
        settings = read_json(args.config)
        steps = build_plan(
            settings, read_json(args.interface), read_json(args.lock),
            args.instructions.read_text(encoding='utf-8'), args.examples_base_url, args.with_training
        )
        if not args.apply:
            print(json.dumps({'mode': 'dry-run', 'steps': steps}, ensure_ascii=False, indent=2, allow_nan=False))
            return 0

        token = settings['api_token'].strip()
        if not token:
            raise ValueError('Set api_token in the config file before using --apply')
        adopted = {}
        for item in args.adopt:
            role, separator, resource_id = item.partition('=')
            if not separator or not role or not resource_id or role in adopted:
                raise ValueError('--adopt expects unique ROLE=ID pairs')
            adopted[role] = resource_id
        state = apply_plan(
            steps, args.state, ApiClient(token, auth_scheme=args.auth_scheme), adopt=adopted or None
        )
        print('Created/verified resources (pools CLOSED; tasks not uploaded):')
        for role, resource in state['resources'].items():
            print(f"  {role}: {resource['id']}")
        print(f'State: {args.state}')
        return 0
    except (OSError, ValueError, ApiError, ProvisioningError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
