"""Read-only structural and repeated-run measurements; not a semantic accuracy score."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--taxonomy', type=Path, required=True)
    parser.add_argument('--assignments', type=Path, required=True)
    parser.add_argument('--repeat', type=Path)
    parser.add_argument('--repeat-resources', type=Path, help='Frozen sample used for this repeat; defaults to the original v3 sample')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    script = Path.home() / '.codex/skills/resource-classifier/scripts/validate_classification.py'
    spec = importlib.util.spec_from_file_location('classification_validator', script)
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    taxonomy = load(args.taxonomy)
    resources = load(args.run_dir / 'resources.json')
    assignments = load(args.assignments)
    tax = validator.taxonomy(taxonomy)
    validation = validator.assignments(taxonomy, resources, assignments, tax['taxonomy_hash'])
    paths = tax['paths']
    counts = Counter(item['category_id'] for item in assignments['items'] if item['category_id'])
    roots = Counter()
    for category_id, count in counts.items():
        roots[paths[category_id][0]] += count
    manifest = load(args.run_dir / 'input-summary.json')
    original = Path(manifest['source'])
    checksum = hashlib.sha256(original.read_bytes()).hexdigest()
    if checksum != manifest['source_sha256']:
        raise ValueError('Original export changed since input snapshot')
    result = {
        'scope': 'Structural coverage and repeat consistency only; no human-approved accuracy claim.',
        'validation': validation, 'source_sha256_unchanged': True,
        'status_counts': dict(Counter(x['status'] for x in assignments['items'])),
        'review_reasons': dict(Counter(x['review_reason'] for x in assignments['items'] if x['status'] == 'needs_review')),
        'root_counts': dict(roots),
        'categories': [{'id': c['id'], 'path': paths[c['id']], 'direct_count': counts[c['id']], 'assignable': c['assignable']}
                       for c in taxonomy['categories']],
        'singleton_assignable_nodes': [key for key, count in counts.items() if count == 1],
    }
    if args.repeat:
        repeat = load(args.repeat)
        repeated_ids = {x['resource_id'] for x in repeat['items']}
        repeated_resources = {'resources': [r for r in resources['resources'] if r['id'] in repeated_ids]}
        validator.assignments(taxonomy, repeated_resources, repeat, tax['taxonomy_hash'])
        sample_file = args.repeat_resources or args.run_dir / 'stability-resources.json'
        sample = load(sample_file)
        expected_ids = {r['id'] for r in sample['resources']}
        if repeated_ids != expected_ids:
            raise ValueError('Repeat result does not cover the frozen independent stability sample')
        original_rows = {r['id']: r for r in resources['resources']}
        if len(expected_ids) != len(sample['resources']) or any(original_rows.get(r['id']) != r for r in sample['resources']):
            raise ValueError('Repeat sample must preserve original resource fields and unique IDs')
        first = {x['resource_id']: x for x in assignments['items']}
        changed = []
        for item in repeat['items']:
            before = first[item['resource_id']]
            if (before['category_id'], before['status']) != (item['category_id'], item['status']):
                changed.append({'resource_id': item['resource_id'], 'before': before, 'after': item})
        result['stability'] = {'total': len(repeat['items']), 'same_category_and_status': len(repeat['items']) - len(changed),
                               'changed': changed, 'sample_file': str(sample_file),
                               'sample_sha256': hashlib.sha256(sample_file.read_bytes()).hexdigest(),
                               'basis': 'Same original resources and taxonomy, changed ordering and batch composition; fresh API requests required.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('categories', 'stability')}, ensure_ascii=False))
    if args.repeat:
        print(json.dumps({'repeat_total': result['stability']['total'], 'repeat_agreement': result['stability']['same_category_and_status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
