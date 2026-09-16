"""8.26 additive estimator repair. Never use duplicate-column errors as migrations."""
import logging

COLUMNS = {
    'company_id': 'BIGINT', 'project_id': 'BIGINT', 'blueprint_scope_item_id': 'BIGINT',
    'trade': 'TEXT', 'description': 'TEXT', 'source_ref': 'TEXT',
    'quantity': 'DOUBLE PRECISION DEFAULT 0', 'unit': "TEXT DEFAULT ''",
    'material_unit_cost': 'DOUBLE PRECISION DEFAULT 0', 'labor_unit_cost': 'DOUBLE PRECISION DEFAULT 0',
    'subcontract_quote': 'DOUBLE PRECISION DEFAULT 0', 'allowance': 'DOUBLE PRECISION DEFAULT 0',
    'markup_pct': 'DOUBLE PRECISION DEFAULT 0', 'notes': "TEXT DEFAULT ''", 'verified': 'INTEGER DEFAULT 0',
    'ai_quantity': 'DOUBLE PRECISION', 'ai_unit': "TEXT DEFAULT ''", 'ai_confidence': "TEXT DEFAULT ''",
    'ai_basis': "TEXT DEFAULT ''", 'ai_source': "TEXT DEFAULT ''", 'ai_updated': 'TEXT',
    'created': 'TEXT', 'updated': 'TEXT',
}


def ensure(runtime):
    c = runtime.db()
    pg = getattr(runtime, 'DATABASE_KIND', 'sqlite') == 'postgres'
    try:
        if pg:
            # Serializes concurrent startup repairs before CREATE as well as ALTER.
            c.execute('SELECT pg_advisory_xact_lock(826001)')
        else:
            c.execute('BEGIN IMMEDIATE')
        key = 'BIGSERIAL PRIMARY KEY' if pg else 'INTEGER PRIMARY KEY'
        c.execute('CREATE TABLE IF NOT EXISTS estimator_items(id '+key+','+
                  ','.join(k+' '+v for k,v in COLUMNS.items())+')')
        if pg:
            for name, kind in COLUMNS.items():
                c.execute('ALTER TABLE estimator_items ADD COLUMN IF NOT EXISTS '+name+' '+kind)
            col = c.execute("""SELECT column_default,is_identity FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name='estimator_items' AND column_name='id'""").fetchone()
            if not col:
                raise RuntimeError('Estimator identity column is missing')
            if not col['column_default'] and col['is_identity'] != 'YES':
                # Only old tables with no default need sequence repair. Do not rewind a live sequence.
                c.execute('LOCK TABLE estimator_items IN ACCESS EXCLUSIVE MODE')
                c.execute('CREATE SEQUENCE IF NOT EXISTS bc826_estimator_items_id_seq')
                c.execute('ALTER SEQUENCE bc826_estimator_items_id_seq OWNED BY estimator_items.id')
                c.execute("SELECT setval('bc826_estimator_items_id_seq',GREATEST(COALESCE((SELECT MAX(id) FROM estimator_items),0)+1,(SELECT last_value+1 FROM bc826_estimator_items_id_seq),1),false)")
                c.execute("ALTER TABLE estimator_items ALTER COLUMN id SET DEFAULT nextval('bc826_estimator_items_id_seq')")
        else:
            present = {r['name'] for r in c.execute('PRAGMA table_info(estimator_items)').fetchall()}
            for name, kind in COLUMNS.items():
                if name not in present:
                    c.execute('ALTER TABLE estimator_items ADD COLUMN '+name+' '+kind)
        c.execute('CREATE INDEX IF NOT EXISTS idx_estimator_project_scope ON estimator_items(company_id,project_id,blueprint_scope_item_id)')
        c.execute('SELECT '+','.join(COLUMNS)+' FROM estimator_items WHERE 1=0')
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def install(runtime):
    def repaired():
        return ensure(runtime)
    runtime._ensure_v34_estimator_tables = repaired
    try:
        repaired()
        return True
    except Exception:
        logging.getLogger('buildcommand.estimator').exception('Estimator schema repair failed')
        return False


def sync(c, cid, pid, run_id, now, insert):
    """Caller owns project write lock and transaction; preserve every entered number."""
    rows = c.execute('''SELECT i.*,s.trade AS scope_trade FROM blueprint_scope_items i
        JOIN blueprint_trade_scopes s ON s.id=i.trade_scope_id AND s.company_id=i.company_id AND s.project_id=i.project_id
        WHERE i.company_id=? AND i.project_id=? AND i.run_id=? AND s.run_id=? ORDER BY i.id''',
        (cid,pid,run_id,run_id)).fetchall()
    added=updated=0
    for row in rows:
        r=dict(row)
        refs=' | '.join(str(r.get(x) or '').strip() for x in ('source_sheet','source_detail','source_spec','source_note') if r.get(x))
        old=c.execute('SELECT * FROM estimator_items WHERE company_id=? AND project_id=? AND blueprint_scope_item_id=? ORDER BY id LIMIT 1',(cid,pid,r['id'])).fetchone()
        if old:
            if (old['trade'],old['description'],old['source_ref']) != (r['scope_trade'],r['requirement'],refs):
                c.execute('UPDATE estimator_items SET trade=?,description=?,source_ref=?,updated=? WHERE id=? AND company_id=? AND project_id=?',
                          (r['scope_trade'],r['requirement'],refs,now,old['id'],cid,pid));updated+=1
        else:
            insert(c,'estimator_items',dict(company_id=cid,project_id=pid,blueprint_scope_item_id=r['id'],trade=r['scope_trade'],description=r['requirement'],source_ref=refs,created=now,updated=now));added+=1
    return dict(added=added,updated=updated,run_id=run_id)
