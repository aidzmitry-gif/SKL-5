-- Additive proposal only. Apply after the two new release tables.
-- Preserve existing reservation_guards.sql. No migration ID is registered here.
CREATE TRIGGER invoice_release_immutable BEFORE UPDATE OR DELETE ON wms.invoice_reservation_release
FOR EACH ROW EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER invoice_release_no_truncate BEFORE TRUNCATE ON wms.invoice_reservation_release
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER invoice_release_line_immutable BEFORE UPDATE OR DELETE ON wms.invoice_reservation_release_line
FOR EACH ROW EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER invoice_release_line_no_truncate BEFORE TRUNCATE ON wms.invoice_reservation_release_line
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_reservation_change();

CREATE FUNCTION wms.check_invoice_release_package() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE release_key integer; p wms.invoice_reservation_release%ROWTYPE; expected integer;
BEGIN
  IF TG_TABLE_NAME = 'invoice_reservation_release' THEN release_key := NEW.id;
  ELSE release_key := NEW.release_id; END IF;
  SELECT * INTO STRICT p FROM wms.invoice_reservation_release WHERE id=release_key;
  SELECT jsonb_array_length(r.snapshot::jsonb->'allocations') INTO expected
    FROM wms.invoice_reservation r WHERE r.document_id=p.document_id AND r.organization_id=p.organization_id
      AND r.digest=p.snapshot->>'reservation_digest';
  IF expected IS NULL OR expected < 1 OR expected <> (
    SELECT count(*) FROM wms.invoice_reservation_release_line WHERE release_id=p.id
  ) OR expected <> jsonb_array_length(p.snapshot::jsonb->'lines') THEN
    RAISE EXCEPTION 'Incomplete invoice release package';
  END IF;
  IF EXISTS (
    SELECT 1 FROM wms.invoice_reservation_release_line l
    JOIN wms.reservation_version b ON b.id=l.before_id
    JOIN wms.reservation_version a ON a.id=l.after_id
    WHERE l.release_id=p.id AND (
      b.organization_id<>p.organization_id OR a.organization_id<>p.organization_id
      OR b.source<>l.source OR a.source<>l.source OR b.sku_code<>a.sku_code
      OR b.warehouse<>a.warehouse OR b.version<>1 OR a.version<>2
      OR b.qty<=0 OR a.qty<>0 OR l.released_qty<>b.qty OR l.line_no<=0
      OR NOT EXISTS (
        SELECT 1 FROM wms.invoice_reservation original,
          LATERAL jsonb_array_elements(original.snapshot::jsonb->'allocations') allocation
        WHERE original.document_id=p.document_id AND original.organization_id=p.organization_id
          AND allocation->>'source'=l.source
          AND (allocation->>'line_no')::integer=l.line_no
          AND allocation->>'sku_code'=b.sku_code
          AND allocation->>'warehouse'=b.warehouse
          AND (allocation->>'qty')::numeric=b.qty
      )
      OR NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(p.snapshot::jsonb->'lines') e
        WHERE e->>'source'=l.source AND (e->>'line_no')::integer=l.line_no
          AND (e->>'before_id')::integer=b.id AND (e->>'after_id')::integer=a.id
          AND e->>'sku_code'=b.sku_code AND e->>'warehouse'=b.warehouse
          AND (e->>'released_qty')::numeric=b.qty
      )
    )
  ) THEN RAISE EXCEPTION 'Invalid invoice release links'; END IF;
  IF NOT EXISTS (SELECT 1 FROM wms.reservation_event_state WHERE document_id=p.document_id AND state='released')
     OR EXISTS (SELECT 1 FROM wms.task t JOIN wms.reservation_pick k ON k.task_id=t.id
                WHERE k.document_id=p.document_id AND
                  (t.organization_id IS DISTINCT FROM p.organization_id OR t.kind<>'pick'
                   OR t.status<>'canceled' OR t.done_at IS NOT NULL)) THEN
    RAISE EXCEPTION 'Invoice release requires terminal private picks';
  END IF;
  RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER complete_invoice_release AFTER INSERT ON wms.invoice_reservation_release
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_invoice_release_package();
CREATE CONSTRAINT TRIGGER complete_invoice_release_line AFTER INSERT ON wms.invoice_reservation_release_line
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_invoice_release_package();

-- Compatible bounded namespace protocol: current reserve v1 + no-shipment release v2.
-- Consumption/partial release is intentionally a future versioned protocol.
-- This guards package consistency; review authority still needs restricted DML/functions.
CREATE FUNCTION wms.check_invoice_reservation_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owner_id integer; allocation jsonb;
BEGIN
  IF NEW.source NOT LIKE 'invoice:%' THEN RETURN NEW; END IF;
  IF NEW.source !~ '^invoice:[0-9]+:[0-9]+:[0-9a-f]{32}$' THEN
    RAISE EXCEPTION 'Invalid invoice reservation namespace';
  END IF;
  owner_id := split_part(NEW.source, ':', 2)::integer;
  SELECT e INTO allocation FROM wms.invoice_reservation r,
    LATERAL jsonb_array_elements(r.snapshot::jsonb->'allocations') e
    WHERE r.document_id=owner_id AND r.organization_id=NEW.organization_id AND e->>'source'=NEW.source;
  IF allocation IS NULL OR allocation->>'sku_code'<>NEW.sku_code
      OR allocation->>'warehouse'<>NEW.warehouse
      OR (allocation->>'line_no')::integer<>split_part(NEW.source, ':', 3)::integer THEN
    RAISE EXCEPTION 'Invoice reservation identity mismatch';
  END IF;
  IF NEW.version=1 AND NEW.qty=(allocation->>'qty')::numeric AND NEW.qty>0 THEN RETURN NEW; END IF;
  IF NEW.version=2 AND NEW.qty=0 AND EXISTS (
    SELECT 1 FROM wms.invoice_reservation_release_line l
    JOIN wms.invoice_reservation_release p ON p.id=l.release_id
    WHERE l.after_id=NEW.id AND l.source=NEW.source
      AND p.organization_id=NEW.organization_id AND p.document_id=owner_id
  ) THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'Invoice quantity change requires supported release evidence';
END $$;
CREATE CONSTRAINT TRIGGER invoice_reservation_insert_protocol AFTER INSERT ON wms.reservation_version
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_invoice_reservation_insert();
