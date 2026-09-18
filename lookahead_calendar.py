"""Deterministic calendar and finish-to-start calculations. No DB, AI or I/O.

Dates are ISO calendar dates in the project's timezone. Duration is inclusive
of the normalized start working day. Zero-day milestones keep their exact date.
Extra working days override the normal week; conflicting exceptions are rejected.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from copy import deepcopy
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math

DEFAULT_CALENDAR = dict(weekdays=[0, 1, 2, 3, 4], holidays=[], extra_workdays=[], timezone='UTC')
MAX_ACTIVITIES = 3000
MAX_DURATION = 1000


def day(value, required=False):
    if value in (None, ''):
        if required: raise ValueError('Choose a date.')
        return None
    if not isinstance(value, str): raise ValueError('Use a date in YYYY-MM-DD format.')
    try: result = date.fromisoformat(value)
    except ValueError: raise ValueError('Use a valid date in YYYY-MM-DD format.') from None
    if result.isoformat() != value or not 1900 <= result.year <= 2199:
        raise ValueError('Use a date between 1900 and 2199 in YYYY-MM-DD format.')
    return result


def integer(value, lo, hi, label):
    if isinstance(value, bool): raise ValueError(f'Enter {label} as a whole number from {lo} to {hi}.')
    text = str(value).strip()
    try: result = int(text)
    except (TypeError, ValueError): raise ValueError(f'Enter {label} as a whole number from {lo} to {hi}.') from None
    if str(result) != text or not lo <= result <= hi:
        raise ValueError(f'Enter {label} as a whole number from {lo} to {hi}.')
    return result


def percent(value):
    try: result = float(value)
    except (TypeError, ValueError): raise ValueError('Progress must be a number from 0 to 100.') from None
    if isinstance(value, bool) or not math.isfinite(result) or not 0 <= result <= 100:
        raise ValueError('Progress must be a number from 0 to 100.')
    return round(result, 2)


@dataclass(frozen=True)
class WorkCalendar:
    weekdays: frozenset
    holidays: frozenset
    extra_workdays: frozenset
    timezone: str = 'UTC'

    @classmethod
    def load(cls, payload):
        if not isinstance(payload, dict): raise ValueError('Choose the project work calendar.')
        week = payload.get('weekdays', [])
        if not isinstance(week, list) or not week or any(type(x) is not int or x not in range(7) for x in week):
            raise ValueError('Choose at least one normal working day.')
        if len(week) != len(set(week)): raise ValueError('Working days must be distinct.')
        exceptions = []
        for key in ('holidays', 'extra_workdays'):
            values = payload.get(key, [])
            if not isinstance(values, list) or len(values) > 366: raise ValueError('Use no more than 366 dates in each exception list.')
            exceptions.append(frozenset(day(v, True) for v in values))
        if exceptions[0] & exceptions[1]: raise ValueError('A date cannot be both nonworking and an extra working day.')
        tz = payload.get('timezone', 'UTC')
        if not isinstance(tz, str) or len(tz) > 80: raise ValueError('Choose a valid project timezone.')
        try: ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError): raise ValueError('Enter a valid project timezone, such as America/Phoenix or UTC.') from None
        return cls(frozenset(week), exceptions[0], exceptions[1], tz)

    def dump(self):
        return dict(weekdays=sorted(self.weekdays), holidays=sorted(d.isoformat() for d in self.holidays),
                    extra_workdays=sorted(d.isoformat() for d in self.extra_workdays), timezone=self.timezone)

    def working(self, d):
        return d not in self.holidays and (d in self.extra_workdays or d.weekday() in self.weekdays)

    def next_work(self, d):
        for _ in range(3700):
            if d.year > 2199: break
            if self.working(d): return d
            d += timedelta(days=1)
        raise ValueError('The working date is outside the supported calendar range.')

    def plan(self, requested, duration):
        start = day(requested, True)
        duration = integer(duration, 0, MAX_DURATION, 'duration in workdays')
        if duration == 0: return start.isoformat(), start.isoformat()
        start = self.next_work(start)
        finish = start
        for _ in range(duration - 1): finish = self.next_work(finish + timedelta(days=1))
        return start.isoformat(), finish.isoformat()

    def successor(self, finish, lag=0):
        lag = integer(lag, 0, 365, 'lag in workdays')
        result = self.next_work(day(finish, True) + timedelta(days=1))
        for _ in range(lag): result = self.next_work(result + timedelta(days=1))
        return result.isoformat()

    def count(self, start, finish):
        a, b = day(start, True), day(finish, True)
        if b < a: raise ValueError('Finish cannot be before start.')
        if (b-a).days > 36525: raise ValueError('Use a date range no longer than 100 years.')
        return sum(self.working(a+timedelta(days=i)) for i in range((b-a).days+1))

    def variance(self, planned, actual):
        a, b = day(planned, True), day(actual, True)
        if a == b: return 0
        lo, hi = min(a,b), max(a,b)
        if (hi-lo).days > 36525: raise ValueError('Use a date range no longer than 100 years.')
        n = sum(self.working(lo+timedelta(days=i)) for i in range(1,(hi-lo).days+1))
        return n if b > a else -n

    def today(self, now=None):
        return (now or datetime.now(ZoneInfo(self.timezone))).astimezone(ZoneInfo(self.timezone)).date()


def completed(row):
    return bool(row.get('actual_finish')) or float(row.get('pct') or 0) >= 100 or str(row.get('status','')).upper() in {'COMPLETE','COMPLETED','DONE'}


def started(row):
    return bool(row.get('actual_start')) or float(row.get('pct') or 0) > 0 or str(row.get('status','')).upper() in {'IN_PROGRESS','STARTED'}


def window(rows, start, weeks, as_of, bucket='lookahead'):
    start = day(start, True); as_of = day(as_of, True)
    if weeks not in (3,6): raise ValueError('Choose a three- or six-week view.')
    finish = start + timedelta(days=weeks*7-1)
    if finish.year > 2199: raise ValueError('The view ends outside the supported date range.')
    if bucket not in {'lookahead','all','carryover','unscheduled','complete'}: raise ValueError('Choose a listed activity view.')
    counts = dict(lookahead=0, all=len(rows), carryover=0, unscheduled=0, complete=0)
    chosen = []
    for row in rows:
        try: a,b = day(row.get('start')),day(row.get('finish'))
        except ValueError: a,b = None,None
        done = completed(row)
        unscheduled = not a or not b or b < a
        overdue = not done and bool(b and b < as_of)
        carry = not done and bool(b and b < start)
        overlap = not unscheduled and a <= finish and b >= start
        # Include actual work overlapping the window, but never imply ongoing
        # work after its completed actual finish.
        try:
            actual_a = day(row.get('actual_start'))
            actual_b = day(row.get('actual_finish')) or (as_of if actual_a else None)
            actual_overlap = actual_a is not None and actual_b is not None and actual_a <= finish and actual_b >= start
        except ValueError: actual_overlap = False
        in_lookahead = overlap or actual_overlap or (not done and (overdue or carry))
        flags = dict(all=True, unscheduled=unscheduled, carryover=not done and (overdue or carry), complete=done, lookahead=in_lookahead)
        for key in counts:
            if key!='all' and flags[key]: counts[key]+=1
        if flags[bucket]: chosen.append(dict(row, overdue=overdue, carryover=carry, unscheduled=unscheduled))
    chosen.sort(key=lambda r:(not (r['overdue'] or r['carryover']), r.get('start') or '9999',r.get('trade') or '',r['key']))
    return chosen,counts,finish.isoformat()


def dependency_order(rows):
    by = {r['key']:r for r in rows}
    if len(by)!=len(rows): raise ValueError('Activity identities must be unique.')
    if len(rows)>MAX_ACTIVITIES: raise ValueError(f'This workspace supports up to {MAX_ACTIVITIES} activities per project.')
    indegree={k:0 for k in by};children={k:[] for k in by}
    for k,r in by.items():
        deps=r.get('predecessors',[])
        if not isinstance(deps,list) or len(deps)>20 or len(deps)!=len(set(deps)): raise ValueError('Choose up to 20 distinct predecessors per activity.')
        for p in deps:
            if p not in by: raise ValueError('A predecessor is missing or belongs to another project. Reopen the activity.')
            if p==k: raise ValueError('An activity cannot depend on itself.')
            children[p].append(k);indegree[k]+=1
    queue=sorted(k for k,n in indegree.items() if n==0);order=[];i=0
    while i<len(queue):
        k=queue[i];i+=1;order.append(k)
        for child in children[k]:
            indegree[child]-=1
            if indegree[child]==0:queue.append(child)
    if len(order)!=len(rows):raise ValueError('These predecessor links create a circular schedule. Remove the loop before saving.')
    return order,children


def reflow(rows,calendar,seeds,calendar_change=False):
    """Return proposed rows only. Never modifies caller data or actual dates.
    Only edited activities and their not-started successors are moved. Existing
    unlinked dates stay unchanged. Earlier predecessor finishes never pull an
    unedited successor earlier than its current start.
    """
    result=deepcopy(rows);by={r['key']:r for r in result};order,children=dependency_order(result)
    dirty=set(seeds);notes=[]
    if calendar_change:dirty.update(r['key'] for r in result if not started(r) and not completed(r) and r.get('duration') is not None)
    for key in order:
        r=by[key]
        if key not in dirty:continue
        is_explicit=key in seeds and not calendar_change
        if (started(r) or completed(r)) and not is_explicit:
            notes.append(f"{r.get('name','Activity')}: already started or complete; planned and actual dates were not automatically moved.")
            continue
        requested=r.get('requested_start') or r.get('start')
        if not requested or r.get('duration') is None:continue
        earliest=requested
        if not is_explicit and r.get('start'):earliest=max(earliest,r['start'])
        for pred in r.get('predecessors',[]):
            end=by[pred].get('finish')
            if not end:raise ValueError('A predecessor has no finish date. Schedule that predecessor first.')
            earliest=max(earliest,calendar.successor(end,r.get('lag',0)))
        r['start'],r['finish']=calendar.plan(earliest,r['duration'])
        dirty.update(children[key])
    return result,notes


def dependency_conflicts(rows,calendar):
    dependency_order(rows);by={r['key']:r for r in rows};out=[]
    for r in rows:
        for key in r.get('predecessors',[]):
            pred=by[key]
            if not pred.get('finish') or not r.get('start'):out.append(f"{r.get('name')}: a linked date is missing.")
            elif r['start']<calendar.successor(pred['finish'],r.get('lag',0)):
                out.append(f"{r.get('name')}: starts before its finish-to-start predecessor {pred.get('name')} allows. Review the dates or remove an incorrect link.")
    return out
