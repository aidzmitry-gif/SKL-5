-- Independent storage checks for the internal primary receipt binding.
CREATE OR REPLACE FUNCTION wms.reject_primary_receipt_binding_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Primary receipt binding is immutable';
END $$;
CREATE TRIGGER immutable_primary_receipt_binding
BEFORE UPDATE OR DELETE ON wms.primary_receipt_binding
FOR EACH ROW EXECUTE FUNCTION wms.reject_primary_receipt_binding_mutation();
CREATE TRIGGER immutable_primary_receipt_binding_truncate
BEFORE TRUNCATE ON wms.primary_receipt_binding
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_primary_receipt_binding_mutation();

CREATE OR REPLACE FUNCTION wms.check_primary_receipt_binding() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  primary_doc procurement.receipt_document%ROWTYPE;
  physical wms.receipt%ROWTYPE;
  primary_body jsonb;
  saved jsonb;
  fact jsonb;
  choice jsonb;
  physical_line wms.receipt_line%ROWTYPE;
  position integer;
  qty numeric;
  total numeric;
  count_lines integer;
  projection jsonb;
  source_position integer;
BEGIN
  PERFORM 1 FROM accounting.organization WHERE id = NEW.organization_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Primary receipt organization does not exist'; END IF;
  SELECT * INTO primary_doc FROM procurement.receipt_document WHERE id = NEW.source_receipt_id FOR UPDATE;
  IF NOT FOUND OR primary_doc.organization_id IS DISTINCT FROM NEW.organization_id
     OR primary_doc.current_version IS DISTINCT FROM NEW.source_version THEN
    RAISE EXCEPTION 'Primary receipt source owner or version mismatch';
  END IF;
  SELECT document::jsonb INTO primary_body FROM procurement.receipt_revision
    WHERE receipt_id = NEW.source_receipt_id AND version = NEW.source_version;
  IF primary_body IS NULL OR NEW.source_snapshot::jsonb->'document' IS DISTINCT FROM primary_body
     OR NEW.source_snapshot->>'organization_id' IS DISTINCT FROM NEW.organization_id::text
     OR NEW.source_snapshot->>'receipt_id' IS DISTINCT FROM NEW.source_receipt_id::text
     OR NEW.source_snapshot->>'version' IS DISTINCT FROM NEW.source_version::text
     OR NEW.command->>'expected_version' IS DISTINCT FROM NEW.source_version::text
     OR NEW.command->>'request_key' IS DISTINCT FROM NEW.request_key
     OR NULLIF(btrim(NEW.actor), '') IS NULL
     OR NULLIF(btrim(NEW.command->>'evidence'), '') IS NULL THEN
    RAISE EXCEPTION 'Primary receipt binding snapshot or command mismatch';
  END IF;
  -- Validate the complete projection, including lines not selected for this delivery.
  -- Otherwise a forged immutable snapshot can poison later partial receipts and QC.
  IF jsonb_typeof(NEW.source_snapshot::jsonb->'lines') IS DISTINCT FROM 'array'
     OR jsonb_array_length(NEW.source_snapshot::jsonb->'lines') IS DISTINCT FROM
        jsonb_array_length(primary_body->'items') THEN
    RAISE EXCEPTION 'Primary receipt source projection mismatch';
  END IF;
  FOR fact, source_position IN
    SELECT value, ordinality::integer FROM jsonb_array_elements(primary_body->'items') WITH ORDINALITY
  LOOP
    projection := NEW.source_snapshot::jsonb->'lines'->(source_position - 1);
    IF projection->'position' IS DISTINCT FROM to_jsonb(source_position)
       OR projection->>'source_line' IS DISTINCT FROM
          format('procurement:receipt:%s:%s:%s', NEW.source_receipt_id, NEW.source_version, source_position)
       OR projection->>'sku' IS DISTINCT FROM fact->>'sku'
       OR projection->>'lot' IS DISTINCT FROM fact->>'lot'
       OR projection->>'unit' IS DISTINCT FROM fact->>'unit'
       OR NULLIF(btrim(fact->>'unit'), '') IS NULL
       OR jsonb_typeof(projection->'quantity') IS DISTINCT FROM 'string'
       OR (projection->>'quantity')::numeric IS DISTINCT FROM (fact->>'quantity')::numeric
       OR (fact->>'quantity')::numeric <= 0
       OR (fact->>'quantity')::numeric >= 1000000000000
       OR (fact->>'quantity')::numeric <> round((fact->>'quantity')::numeric, 2) THEN
      RAISE EXCEPTION 'Primary receipt source projection mismatch';
    END IF;
  END LOOP;
  SELECT * INTO physical FROM wms.receipt WHERE id = NEW.receipt_id FOR UPDATE;
  IF NOT FOUND OR physical.organization_id IS DISTINCT FROM NEW.organization_id
     OR physical.warehouse IS DISTINCT FROM NEW.command->>'warehouse'
     OR physical.status <> 'pending_qc' OR physical.source <> 'procurement' THEN
    RAISE EXCEPTION 'Physical receipt owner or preparation state mismatch';
  END IF;
  IF EXISTS (SELECT 1 FROM wms.primary_receipt_binding b
    WHERE b.organization_id = NEW.organization_id AND b.source_receipt_id = NEW.source_receipt_id
      AND (b.source_version <> NEW.source_version OR b.source_snapshot::jsonb <> NEW.source_snapshot::jsonb)) THEN
    RAISE EXCEPTION 'Primary receipt revision requires reconciliation';
  END IF;
  IF jsonb_typeof(NEW.line_bindings::jsonb) IS DISTINCT FROM 'array'
     OR jsonb_typeof(NEW.command::jsonb->'lines') IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION 'Primary receipt binding lines required';
  END IF;
  count_lines := jsonb_array_length(NEW.line_bindings::jsonb);
  IF count_lines < 1 OR count_lines <> jsonb_array_length(NEW.command::jsonb->'lines')
     OR count_lines <> (SELECT count(*) FROM wms.receipt_line WHERE receipt_id = NEW.receipt_id)
     OR count_lines <> (SELECT count(DISTINCT item->>'position') FROM jsonb_array_elements(NEW.line_bindings::jsonb) item)
     OR count_lines <> (SELECT count(DISTINCT item->>'receipt_line_id') FROM jsonb_array_elements(NEW.line_bindings::jsonb) item) THEN
    RAISE EXCEPTION 'Primary receipt binding line set mismatch';
  END IF;
  FOR saved IN SELECT * FROM jsonb_array_elements(NEW.line_bindings::jsonb) LOOP
    position := (saved->>'position')::integer;
    qty := (saved->>'quantity')::numeric;
    fact := primary_body->'items'->(position - 1);
    SELECT item INTO choice FROM jsonb_array_elements(NEW.command::jsonb->'lines') item
      WHERE (item->>'position')::integer = position;
    IF fact IS NULL OR position < 1 OR choice IS NULL OR qty IS NULL OR qty <= 0 OR qty >= 1000000000000
       OR qty <> round(qty, 2) OR NULLIF(btrim(fact->>'unit'), '') IS NULL
       OR saved->>'unit' IS DISTINCT FROM fact->>'unit'
       OR saved->>'sku_code' IS DISTINCT FROM choice->>'sku_code'
       OR qty IS DISTINCT FROM (choice->>'quantity')::numeric
       OR saved->>'source_line' IS DISTINCT FROM
          format('procurement:receipt:%s:%s:%s', NEW.source_receipt_id, NEW.source_version, position) THEN
      RAISE EXCEPTION 'Primary receipt line facts mismatch';
    END IF;
    SELECT * INTO physical_line FROM wms.receipt_line WHERE id = (saved->>'receipt_line_id')::integer;
    IF NOT FOUND OR physical_line.receipt_id IS DISTINCT FROM NEW.receipt_id
       OR physical_line.sku_code IS DISTINCT FROM saved->>'sku_code'
       OR physical_line.expected_qty IS DISTINCT FROM qty
       OR physical_line.batch_ref IS DISTINCT FROM fact->>'lot'
       OR NOT EXISTS (SELECT 1 FROM public.sku s WHERE s.code = physical_line.sku_code AND s.unit = fact->>'unit') THEN
      RAISE EXCEPTION 'Physical receipt line differs from primary binding';
    END IF;
    SELECT coalesce(sum((item->>'quantity')::numeric), 0) INTO total
      FROM wms.primary_receipt_binding b CROSS JOIN LATERAL jsonb_array_elements(b.line_bindings::jsonb) item
      WHERE b.organization_id = NEW.organization_id AND b.source_receipt_id = NEW.source_receipt_id
        AND (item->>'position')::integer = position;
    IF total + qty > (fact->>'quantity')::numeric THEN
      RAISE EXCEPTION 'Physical receipts exceed primary line quantity';
    END IF;
  END LOOP;
  RETURN NEW;
END $$;
CREATE TRIGGER check_primary_receipt_binding BEFORE INSERT ON wms.primary_receipt_binding
FOR EACH ROW EXECUTE FUNCTION wms.check_primary_receipt_binding();

CREATE OR REPLACE FUNCTION wms.guard_bound_receipt_line() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE old_receipt integer; new_receipt integer; bound_receipt integer; current_status text;
BEGIN
  IF TG_OP <> 'INSERT' THEN old_receipt := OLD.receipt_id; END IF;
  IF TG_OP <> 'DELETE' THEN new_receipt := NEW.receipt_id; END IF;
  -- Match the application's organization-before-receipt lock order.
  PERFORM o.id FROM accounting.organization o
    WHERE o.id IN (SELECT organization_id FROM wms.receipt WHERE id IN (old_receipt, new_receipt))
    ORDER BY o.id FOR UPDATE;
  SELECT b.receipt_id INTO bound_receipt FROM wms.primary_receipt_binding b
    WHERE b.receipt_id IN (old_receipt, new_receipt) ORDER BY b.receipt_id LIMIT 1;
  IF bound_receipt IS NULL THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
  END IF;
  SELECT status INTO current_status FROM wms.receipt WHERE id = bound_receipt FOR UPDATE;
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'Bound physical receipt line set is immutable';
  END IF;
  IF current_status <> 'pending_qc' THEN
    RAISE EXCEPTION 'Accepted physical receipt lines are immutable';
  END IF;
  IF (to_jsonb(NEW) - ARRAY['accepted_qty', 'rejected_qty', 'reject_reason', 'location_id'])
      IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['accepted_qty', 'rejected_qty', 'reject_reason', 'location_id']) THEN
    RAISE EXCEPTION 'Bound physical receipt line identity is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_bound_receipt_line BEFORE INSERT OR UPDATE OR DELETE ON wms.receipt_line
FOR EACH ROW EXECUTE FUNCTION wms.guard_bound_receipt_line();

CREATE OR REPLACE FUNCTION wms.guard_bound_receipt() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM id FROM accounting.organization WHERE id = OLD.organization_id FOR UPDATE;
  IF NOT EXISTS (SELECT 1 FROM wms.primary_receipt_binding WHERE receipt_id = OLD.id) THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Bound physical receipt is immutable'; END IF;
  IF (to_jsonb(NEW) - ARRAY['status', 'decided_by', 'decided_at']) IS DISTINCT FROM
     (to_jsonb(OLD) - ARRAY['status', 'decided_by', 'decided_at']) THEN
    RAISE EXCEPTION 'Bound physical receipt identity is immutable';
  END IF;
  IF OLD.status <> 'pending_qc' AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
    RAISE EXCEPTION 'Accepted physical receipt is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_bound_receipt BEFORE UPDATE OR DELETE ON wms.receipt
FOR EACH ROW EXECUTE FUNCTION wms.guard_bound_receipt();

CREATE OR REPLACE FUNCTION wms.reject_bound_receipt_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM wms.primary_receipt_binding) THEN
    RAISE EXCEPTION 'Bound physical receipt history is immutable';
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER guard_bound_receipt_line_truncate BEFORE TRUNCATE ON wms.receipt_line
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_bound_receipt_truncate();

CREATE OR REPLACE FUNCTION wms.verify_primary_receipt_state(target_id integer) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE r wms.receipt%ROWTYPE; b wms.primary_receipt_binding%ROWTYPE;
BEGIN
  SELECT * INTO b FROM wms.primary_receipt_binding WHERE receipt_id = target_id;
  IF NOT FOUND THEN RETURN; END IF;
  PERFORM id FROM accounting.organization WHERE id = b.organization_id FOR UPDATE;
  SELECT * INTO r FROM wms.receipt WHERE id = target_id FOR UPDATE;
  IF r.status = 'pending_qc' THEN
    IF EXISTS (SELECT 1 FROM wms.stock_movement WHERE organization_id = r.organization_id
      AND doc_ref = r.number AND reason = 'receipt') THEN
      RAISE EXCEPTION 'Pending primary receipt cannot have physical arrival movements';
    END IF;
    RETURN;
  END IF;
  IF r.status <> 'accepted' THEN RAISE EXCEPTION 'Unsupported bound receipt status'; END IF;
  IF NOT EXISTS (SELECT 1 FROM procurement.receipt_document WHERE id = b.source_receipt_id
    AND organization_id = b.organization_id AND current_version = b.source_version) THEN
    RAISE EXCEPTION 'Primary receipt version changed before acceptance';
  END IF;
  IF EXISTS (SELECT 1 FROM wms.receipt_line WHERE receipt_id = target_id AND
    (accepted_qty IS NULL OR rejected_qty IS NULL OR accepted_qty < 0 OR rejected_qty < 0
     OR accepted_qty + rejected_qty <> expected_qty)) THEN
    RAISE EXCEPTION 'Primary receipt requires complete explicit QC';
  END IF;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(b.line_bindings::jsonb) item
    WHERE NOT EXISTS (SELECT 1 FROM public.sku s WHERE s.code = item->>'sku_code' AND s.unit = item->>'unit')) THEN
    RAISE EXCEPTION 'Primary receipt warehouse unit changed';
  END IF;
  IF EXISTS (
    (SELECT sku_code, r.warehouse, accepted_qty, 'in'::text, location_id, batch_ref
       FROM wms.receipt_line WHERE receipt_id = target_id AND accepted_qty > 0
     EXCEPT ALL
     SELECT sku_code, warehouse, qty, kind::text, location_id, batch_ref
       FROM wms.stock_movement WHERE organization_id = r.organization_id AND doc_ref = r.number AND reason = 'receipt')
    UNION ALL
    (SELECT sku_code, warehouse, qty, kind::text, location_id, batch_ref
       FROM wms.stock_movement WHERE organization_id = r.organization_id AND doc_ref = r.number AND reason = 'receipt'
     EXCEPT ALL
     SELECT sku_code, r.warehouse, accepted_qty, 'in'::text, location_id, batch_ref
       FROM wms.receipt_line WHERE receipt_id = target_id AND accepted_qty > 0)
  ) THEN
    RAISE EXCEPTION 'Primary receipt arrival movements do not match accepted QC';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION wms.check_primary_receipt_state_trigger() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'primary_receipt_binding' THEN
    PERFORM wms.verify_primary_receipt_state(NEW.receipt_id);
  ELSE
    PERFORM wms.verify_primary_receipt_state(NEW.id);
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER complete_primary_receipt_state AFTER UPDATE ON wms.receipt
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_primary_receipt_state_trigger();
CREATE CONSTRAINT TRIGGER complete_primary_receipt_binding_state AFTER INSERT ON wms.primary_receipt_binding
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_primary_receipt_state_trigger();

CREATE OR REPLACE FUNCTION wms.check_primary_receipt_movement_trigger() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE target_id integer;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    FOR target_id IN SELECT r.id FROM wms.receipt r JOIN wms.primary_receipt_binding b ON b.receipt_id = r.id
      WHERE r.organization_id = OLD.organization_id AND r.number = OLD.doc_ref LOOP
      PERFORM wms.verify_primary_receipt_state(target_id);
    END LOOP;
  END IF;
  IF TG_OP <> 'DELETE' THEN
    FOR target_id IN SELECT r.id FROM wms.receipt r JOIN wms.primary_receipt_binding b ON b.receipt_id = r.id
      WHERE r.organization_id = NEW.organization_id AND r.number = NEW.doc_ref LOOP
      PERFORM wms.verify_primary_receipt_state(target_id);
    END LOOP;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER complete_primary_receipt_movements AFTER INSERT OR UPDATE OR DELETE ON wms.stock_movement
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_primary_receipt_movement_trigger();
CREATE TRIGGER guard_bound_receipt_movement_truncate BEFORE TRUNCATE ON wms.stock_movement
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_bound_receipt_truncate();
CREATE TRIGGER guard_bound_receipt_truncate BEFORE TRUNCATE ON wms.receipt
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_bound_receipt_truncate();
