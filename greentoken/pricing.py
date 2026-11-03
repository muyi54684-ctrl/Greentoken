"""Config-only monetary pricing. No API calls and no mutation of usage."""
import math
from datetime import datetime, date
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRICE_FIELDS = ('input_uncached_price', 'input_cached_price', 'output_price')
COST_FIELDS = ('request_timestamp', 'price_source', 'price_checked_at', 'pricing_period',
               'pricing_timezone', *PRICE_FIELDS, 'input_cost', 'output_cost',
               'estimated_api_cost', 'currency', 'cost_estimation_method', 'cost_unavailable_reason',
               'cached_input_tokens', 'uncached_input_tokens', 'pricing_config_snapshot')

def count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0

def rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return Decimal(str(value))

def minute(text):
    if not isinstance(text, str): raise ValueError('Invalid schedule time')
    parts = text.split(':')
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2: raise ValueError('Use HH:MM')
    h,m = map(int,parts)
    if not (0 <= h <= 24 and 0 <= m <= 59) or (h == 24 and m): raise ValueError('Invalid time')
    return 60*h+m

def select_prices(model, cfg, timestamp):
    prices = cfg.get('prices') or {}
    entry = prices.get(model) if isinstance(prices, dict) else None
    if not isinstance(entry, dict): return None, None, 'model_price_missing'
    # Backward-compatible flat v0.1 price names are handled below.
    if any(k in entry for k in ('input_uncached_per_million','input_per_million','output_per_million')):
        return entry, 'standard', None
    rules = cfg.get('pricing') or {}
    try:
        if timestamp.tzinfo is None: return None,None,'request_timestamp_has_no_timezone'
        timezone = rules.get('timezone')
        if not timezone: return None,None,'pricing_timezone_missing'
        local = timestamp.astimezone(ZoneInfo(timezone))
        now = local.hour*60+local.minute
        calendar = rules.get('calendar') or {}
        if rules.get('requires_calendar'):
            if not calendar.get('source') or not calendar.get('checked_at'):
                return None,None,'holiday_calendar_unconfirmed'
            first=date.fromisoformat(calendar['valid_from']);last=date.fromisoformat(calendar['valid_until'])
            if not first <= local.date() <= last:
                return None,None,'request_date_outside_confirmed_calendar'
            dates=calendar.get('off_peak_dates')
            if not isinstance(dates,list): raise ValueError('Calendar dates must be a list')
            confirmed={date.fromisoformat(d) for d in dates}
            if local.date() in confirmed:
                period=rules.get('holiday_period')
                selected=entry.get(period)
                return (selected if isinstance(selected,dict) else None),period,None if isinstance(selected,dict) else 'holiday_period_price_missing'
        periods = rules.get('periods')
        if not isinstance(periods,list) or not periods: return None,None,'pricing_schedule_missing'
        windows=[]
        for period in periods:
            name=period['name']; start=minute(period['start']); end=minute(period['end'])
            if not isinstance(name,str) or not name or start==end or start==1440: raise ValueError('Invalid period')
            weekdays=period.get('weekdays',list(range(7)))
            if not isinstance(weekdays,list) or not weekdays or any(not isinstance(x,int) or isinstance(x,bool) or x<0 or x>6 for x in weekdays):
                raise ValueError('Weekdays must be 0..6')
            windows.append((name,start,end,weekdays))
        matches=[name for name,start,end,weekdays in windows if local.weekday() in weekdays and (start <= now < end if start < end else now >= start or now < end)]
        if not matches and rules.get('default_period'):
            matches=[rules['default_period']]
        if len(matches)!=1: return None,None,'pricing_period_ambiguous_or_uncovered'
        selected = entry.get(matches[0])
        return (selected if isinstance(selected,dict) else None),matches[0],None if isinstance(selected,dict) else 'period_price_missing'
    except (ValueError, KeyError, TypeError, ZoneInfoNotFoundError):
        return None,None,'invalid_pricing_schedule_or_timezone'

def calculate_cost(usage, model, cfg, request_time):
    rules=cfg.get('pricing') or {}
    if not isinstance(rules,dict): rules={}
    result={k:None for k in COST_FIELDS}
    result.update(request_timestamp=request_time.isoformat(), price_source=cfg.get('price_source'),
                  price_checked_at=cfg.get('price_checked_at'), currency=cfg.get('currency'),
                  pricing_timezone=rules.get('timezone'), cost_estimation_method='unavailable',
                  pricing_config_snapshot={'model_prices':(cfg.get('prices') or {}).get(model) if isinstance(cfg.get('prices') or {},dict) else None, 'pricing':rules})
    def unavailable(reason):
        result['cost_unavailable_reason']=reason
        return result
    price,period,error=select_prices(model,cfg,request_time)
    result['pricing_period']=period
    if error: return unavailable(error)
    uncached=price.get('input_uncached_per_million',price.get('input_per_million'))
    cached=price.get('input_cached_per_million',price.get('cached_input_per_million'))
    output=price.get('output_per_million')
    for key,value in zip(PRICE_FIELDS,(uncached,cached,output)):
        result[key]=value if rate(value) is not None else None
    if not cfg.get('price_source') or not cfg.get('price_checked_at') or not cfg.get('currency'):
        return unavailable('price_metadata_missing')
    if not isinstance(usage,dict): return unavailable('usage_missing')
    i,o=usage.get('prompt_tokens'),usage.get('completion_tokens')
    if not count(i) or not count(o): return unavailable('invalid_or_missing_token_usage')
    hit,miss=usage.get('prompt_cache_hit_tokens'),usage.get('prompt_cache_miss_tokens')
    details=usage.get('prompt_tokens_details') or {}
    nested=details.get('cached_tokens') if isinstance(details,dict) else None
    if any(v is not None and not count(v) for v in (hit,miss,nested)):
        return unavailable('invalid_cache_usage')
    if hit is not None and nested is not None and hit!=nested:
        return unavailable('conflicting_cache_usage')
    hit=hit if hit is not None else nested
    method='exact_from_usage_breakdown'
    if hit is None and miss is None:
        if rules.get('missing_cache_usage','unavailable')!='assume_uncached_input':
            return unavailable('cache_usage_missing')
        hit,miss=0,i
        method='assume_uncached_input'
    elif hit is None:
        hit=i-miss
    elif miss is None:
        miss=i-hit
    if not count(hit) or not count(miss) or hit+miss!=i:
        return unavailable('cache_breakdown_does_not_match_prompt_tokens')
    result.update(cached_input_tokens=hit,uncached_input_tokens=miss)
    u,c,out=rate(uncached),rate(cached),rate(output)
    # Null is an unconfirmed price even when the corresponding token count is zero.
    if u is None or c is None or out is None: return unavailable('price_null_or_invalid')
    ic=(Decimal(miss)*u+Decimal(hit)*c)/Decimal(1_000_000)
    oc=Decimal(o)*out/Decimal(1_000_000)
    result.update(input_cost=float(ic),output_cost=float(oc),estimated_api_cost=float(ic+oc),
                  cost_estimation_method=method,cost_unavailable_reason=None)
    return result

def format_cost(value, currency):
    if value is None: return None
    symbol={'CNY':'¥','USD':'$'}.get(currency, (currency+' ') if currency else '')
    # Fixed notation with all stored significant digits, never two-decimal rounding.
    return symbol+format(Decimal(str(value)),'f')
