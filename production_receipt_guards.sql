-- Preserve receipt provenance independently of editable QC lines.
CREATE OR REPLACE FUNCTION wms.guard_production_receipt_source() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE d record;
DECLARE anchored boolean;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    SELECT EXISTS (SELECT 1 FROM production.output_confirmation WHERE event_id=OLD.source_event_id)
      INTO anchored;
    IF anchored THEN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Confirmed production receipt source is immutable'; END IF;
      IF ROW(NEW.id,NEW.source_event_id,NEW.source,NEW.entity_ref,NEW.organization_id,NEW.warehouse)
        IS DISTINCT FROM ROW(OLD.id,OLD.source_event_id,OLD.source,OLD.entity_ref,OLD.organization_id,OLD.warehouse)
        OR (coalesce(OLD.number,'')<>'' AND NEW.number IS DISTINCT FROM OLD.number)
        OR (OLD.status='accepted' AND NEW.status IS DISTINCT FROM OLD.status) THEN
        RAISE EXCEPTION 'Confirmed production receipt source is immutable';
      END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  END IF;
  SELECT doc.* INTO d FROM production.output_confirmation c
    JOIN production.output_document doc ON doc.id=c.document_id WHERE c.event_id=NEW.source_event_id;
  IF FOUND THEN
    IF NEW.organization_id IS DISTINCT FROM d.organization_id
      OR NEW.source IS DISTINCT FROM 'production'
      OR NEW.entity_ref IS DISTINCT FROM 'production_output:'||d.id
      OR NEW.warehouse IS DISTINCT FROM d.command->>'warehouse'
      OR (TG_OP='INSERT' AND NEW.status IS DISTINCT FROM 'pending_qc') THEN
      RAISE EXCEPTION 'Production receipt source differs from its confirmation';
    END IF;
  ELSIF NEW.entity_ref LIKE 'production_output:%'
    OR EXISTS (SELECT 1 FROM public.outbox_event WHERE id=NEW.source_event_id AND event_type='production.output.confirmed') THEN
    RAISE EXCEPTION 'Production receipt requires a confirmed output source';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_production_receipt_source BEFORE INSERT OR UPDATE OR DELETE ON wms.receipt
FOR EACH ROW EXECUTE FUNCTION wms.guard_production_receipt_source();

CREATE OR REPLACE FUNCTION wms.guard_production_receipt_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM wms.receipt r JOIN production.output_confirmation c ON c.event_id=r.source_event_id) THEN
    RAISE EXCEPTION 'Confirmed production receipt source is immutable';
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER guard_production_receipt_truncate BEFORE TRUNCATE ON wms.receipt
FOR EACH STATEMENT EXECUTE FUNCTION wms.guard_production_receipt_truncate();

CREATE OR REPLACE FUNCTION wms.guard_accepted_production_line() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE old_receipt integer;
DECLARE new_receipt integer;
DECLARE r record;
BEGIN
  IF TG_OP<>'INSERT' THEN old_receipt := OLD.receipt_id; END IF;
  IF TG_OP<>'DELETE' THEN new_receipt := NEW.receipt_id; END IF;
  -- Lock the header even while pending, then inspect its post-wait status.
  -- This serializes an external line edit with the receipt acceptance command.
  FOR r IN SELECT id,status,source_event_id FROM wms.receipt
    WHERE id IN (old_receipt,new_receipt) ORDER BY id FOR UPDATE LOOP
    IF r.status='accepted' AND EXISTS (SELECT 1 FROM production.output_confirmation c WHERE c.event_id=r.source_event_id) THEN
      RAISE EXCEPTION 'Accepted production receipt lines are immutable';
    END IF;
  END LOOP;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_accepted_production_line BEFORE INSERT OR UPDATE OR DELETE ON wms.receipt_line
FOR EACH ROW EXECUTE FUNCTION wms.guard_accepted_production_line();

CREATE OR REPLACE FUNCTION wms.guard_accepted_production_lines_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM wms.receipt r JOIN production.output_confirmation c ON c.event_id=r.source_event_id
    WHERE r.status='accepted') THEN
    RAISE EXCEPTION 'Accepted production receipt lines are immutable';
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER guard_accepted_production_lines_truncate BEFORE TRUNCATE ON wms.receipt_line
FOR EACH STATEMENT EXECUTE FUNCTION wms.guard_accepted_production_lines_truncate();
