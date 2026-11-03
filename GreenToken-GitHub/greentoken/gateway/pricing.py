"""Versioned Gateway reference prices; never reads experiment price calendars."""
import copy
import hashlib
import json
import math
from datetime import date
from pathlib import Path

PRICE_KEYS = ('input_uncached_per_million', 'input_cached_per_million', 'output_per_million')


def select_gateway_pricing(document):
    if not isinstance(document, dict) or type(document.get('schema_version')) is not int or document.get('schema_version') != 1:
        raise ValueError('Unsupported Gateway pricing schema')
    versions = document.get('versions')
    if not isinstance(versions, list) or not versions or any(not isinstance(v, dict) for v in versions):
        raise ValueError('Gateway pricing versions must be a non-empty list')
    ids = [v.get('version') for v in versions]
    if any(not isinstance(v, str) or not v for v in ids) or len(ids) != len(set(ids)):
        raise ValueError('Gateway pricing version IDs must be unique')
    active = document.get('active_version')
    if active not in ids:
        raise ValueError('Gateway active pricing version not found')
    profile = copy.deepcopy(versions[ids.index(active)])
    if profile.get('policy') not in ('fixed_peak_reference', 'fixed_reference_rates'):
        raise ValueError('Unsupported Gateway pricing policy')
    if not profile.get('currency') or not profile.get('price_source'):
        raise ValueError('Gateway pricing currency/source missing')
    try:
        date.fromisoformat(profile['checked_at'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('Gateway pricing checked_at must be an ISO date') from None
    if profile.get('missing_cache_usage') not in ('unavailable', 'assume_uncached_input'):
        raise ValueError('Invalid Gateway cache estimation policy')
    models = profile.get('models')
    if not isinstance(models, dict):
        raise ValueError('Gateway model prices must be a mapping')
    for rates in models.values():
        if not isinstance(rates, dict):
            raise ValueError('Invalid Gateway model prices')
        for key in PRICE_KEYS:
            value = rates.get(key)
            # Null/missing rates remain unconfirmed; the existing calculator returns unavailable.
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                raise ValueError('Gateway prices must be nonnegative numbers or null')
    profile['signature'] = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return profile


def load_gateway_pricing(path=None):
    path = Path(path) if path is not None else Path(__file__).with_name('pricing.json')
    return select_gateway_pricing(json.loads(path.read_text(encoding='utf-8')))


def apply_gateway_pricing(experiment_config, profile):
    """Copy only pricing overrides into the ephemeral Engine's config."""
    result = copy.deepcopy(experiment_config)
    result.update(prices=copy.deepcopy(profile['models']), currency=profile['currency'],
                  price_source=profile['price_source'], price_checked_at=profile['checked_at'],
                  pricing={'missing_cache_usage': profile['missing_cache_usage']},
                  gateway_pricing=copy.deepcopy(profile))
    # Flat rates reuse the unchanged monetary calculator without time-period selection.
    return result
