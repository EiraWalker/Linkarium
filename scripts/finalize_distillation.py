"""Merge validated, targeted API rechecks without retaining superseded topic support."""
import argparse
import json
from pathlib import Path
import resource_classification_run as runner


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run_dir', type=Path)
    args = p.parse_args()
    root = args.run_dir
    read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
    resources = read(root / 'resources.json')
    original = read(root / 'distill-v2/combined.json')
    rechecked = read(root / 'distill-recheck/combined.json')
    expected = {r['id'] for r in read(root / 'distill-recheck-resources.json')['resources']}
    replacements = {r['resource_id']: r for r in rechecked['items']}
    if replacements.keys() != expected:
        raise ValueError('Incomplete targeted recheck; do not silently keep unsupported originals')
    combined = {'items': [replacements.get(r['resource_id'], r) for r in original['items']], 'candidate_topics': []}
    for topic in original['candidate_topics']:
        supports = [key for key in topic['support_ids'] if key not in replacements]
        if supports:
            combined['candidate_topics'].append({**topic, 'support_ids': supports})
    combined['candidate_topics'].extend(rechecked['candidate_topics'])
    validator = runner.load_validator(runner.DEFAULT_SKILL)
    validation = runner.validate_distillation(validator, {'batch_id': 'whole-library', **resources}, {'batch_id': 'whole-library', **combined})
    output = root / 'distill-final'
    runner.write_json(output / 'combined.json', combined)
    runner.write_json(output / 'provenance.json', {'original': 'distill-v2/combined.json', 'rechecked': 'distill-recheck/combined.json',
                                                'replaced_ids': sorted(replacements), 'validation': validation,
                                                'note': 'Recheck outputs supersede the earlier four items; other items retain original API provenance.'})
    print(json.dumps(validation, ensure_ascii=False))


if __name__ == '__main__':
    main()
