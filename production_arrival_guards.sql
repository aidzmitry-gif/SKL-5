CREATE FUNCTION wms.reject_production_arrival_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Production arrival binding is immutable'; END $$;
CREATE TRIGGER immutable_production_arrival BEFORE UPDATE OR DELETE OR TRUNCATE ON wms.production_arrival
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_production_arrival_mutation();

CREATE FUNCTION wms.check_production_arrival_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r wms.receipt%ROWTYPE; l wms.receipt_line%ROWTYPE; m wms.stock_movement%ROWTYPE;
BEGIN
  SELECT * INTO r FROM wms.receipt WHERE id=NEW.receipt_id FOR UPDATE;
  IF NOT FOUND OR r.status<>'accepted' OR r.organization_id IS DISTINCT FROM NEW.organization_id
    OR r.source_event_id IS DISTINCT FROM NEW.event_id
    OR NOT EXISTS (SELECT 1 FROM production.output_confirmation WHERE event_id=NEW.event_id AND organization_id=NEW.organization_id) THEN
    RAISE EXCEPTION 'Production arrival receipt identity mismatch';
  END IF;
  SELECT * INTO l FROM wms.receipt_line WHERE id=NEW.line_id;
  IF NOT FOUND OR l.receipt_id IS DISTINCT FROM r.id
    OR (SELECT count(*) FROM wms.receipt_line WHERE receipt_id=r.id)<>1
    OR NEW.accepted_quantity IS DISTINCT FROM l.accepted_qty OR NEW.rejected_quantity IS DISTINCT FROM l.rejected_qty
    OR l.accepted_qty IS NULL OR l.rejected_qty IS NULL OR l.accepted_qty<0 OR l.rejected_qty<0
    OR l.accepted_qty+l.rejected_qty IS DISTINCT FROM l.expected_qty THEN
    RAISE EXCEPTION 'Production arrival QC mismatch';
  END IF;
  IF NEW.accepted_quantity=0 THEN
    IF NEW.movement_id IS NOT NULL THEN RAISE EXCEPTION 'Rejected output cannot have an arrival movement'; END IF;
  ELSE
    SELECT * INTO m FROM wms.stock_movement WHERE id=NEW.movement_id;
    IF NOT FOUND OR ROW(m.organization_id,m.doc_ref,m.reason,m.sku_code,m.warehouse,m.kind,m.qty,m.location_id,m.batch_ref)
      IS DISTINCT FROM ROW(r.organization_id,r.number,'receipt',l.sku_code,r.warehouse,'in',l.accepted_qty,l.location_id,l.batch_ref) THEN
      RAISE EXCEPTION 'Production arrival movement mismatch';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER check_production_arrival_insert BEFORE INSERT ON wms.production_arrival
FOR EACH ROW EXECUTE FUNCTION wms.check_production_arrival_insert();

CREATE FUNCTION wms.protect_production_arrival_movement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='TRUNCATE' THEN
    IF EXISTS (SELECT 1 FROM wms.production_arrival WHERE movement_id IS NOT NULL) THEN
      RAISE EXCEPTION 'Production arrival movement is immutable'; END IF;
  ELSIF EXISTS (SELECT 1 FROM wms.production_arrival WHERE movement_id=OLD.id) THEN
    RAISE EXCEPTION 'Production arrival movement is immutable';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER protect_production_arrival_movement BEFORE UPDATE OR DELETE ON wms.stock_movement
FOR EACH ROW EXECUTE FUNCTION wms.protect_production_arrival_movement();
CREATE TRIGGER protect_production_arrival_movement_truncate BEFORE TRUNCATE ON wms.stock_movement
FOR EACH STATEMENT EXECUTE FUNCTION wms.protect_production_arrival_movement();

CREATE FUNCTION wms.check_production_arrival_state(target_id integer) RETURNS void LANGUAGE plpgsql AS $$
DECLARE r wms.receipt%ROWTYPE; b wms.production_arrival%ROWTYPE;
BEGIN
  SELECT * INTO r FROM wms.receipt WHERE id=target_id;
  IF NOT FOUND OR NOT EXISTS (SELECT 1 FROM production.output_confirmation WHERE event_id=r.source_event_id) THEN RETURN; END IF;
  PERFORM 1 FROM accounting.organization WHERE id=r.organization_id FOR UPDATE;
  SELECT * INTO r FROM wms.receipt WHERE id=target_id FOR UPDATE;
  IF r.status='accepted' THEN
    SELECT * INTO b FROM wms.production_arrival WHERE receipt_id=r.id;
    IF NOT FOUND THEN RAISE EXCEPTION 'Accepted production receipt requires its arrival binding'; END IF;
    IF EXISTS (SELECT 1 FROM wms.stock_movement WHERE organization_id=r.organization_id
      AND doc_ref=r.number AND reason='receipt' AND id IS DISTINCT FROM b.movement_id) THEN
      RAISE EXCEPTION 'Production arrival has extra movements';
    END IF;
  ELSIF EXISTS (SELECT 1 FROM wms.stock_movement WHERE organization_id=r.organization_id AND doc_ref=r.number AND reason='receipt') THEN
    RAISE EXCEPTION 'Unaccepted production receipt cannot have arrival movements';
  END IF;
END $$;

CREATE FUNCTION wms.require_production_arrival() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM wms.check_production_arrival_state(NEW.id);
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER require_production_arrival AFTER INSERT OR UPDATE ON wms.receipt
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.require_production_arrival();

CREATE FUNCTION wms.check_production_movement_set() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
  IF TG_OP<>'INSERT' THEN
    FOR r IN SELECT id FROM wms.receipt WHERE organization_id=OLD.organization_id AND number=OLD.doc_ref LOOP
      PERFORM wms.check_production_arrival_state(r.id);
    END LOOP;
  END IF;
  IF TG_OP<>'DELETE' THEN
    FOR r IN SELECT id FROM wms.receipt WHERE organization_id=NEW.organization_id AND number=NEW.doc_ref LOOP
      PERFORM wms.check_production_arrival_state(r.id);
    END LOOP;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER check_production_movement_set AFTER INSERT OR UPDATE OR DELETE ON wms.stock_movement
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_production_movement_set();
