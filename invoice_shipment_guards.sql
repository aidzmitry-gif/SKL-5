-- Unallocated additive packet. Run after ORM tables + existing release guards.
CREATE TRIGGER physical_act_immutable BEFORE UPDATE OR DELETE ON wms.physical_shipment_act
FOR EACH ROW EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER physical_act_no_truncate BEFORE TRUNCATE ON wms.physical_shipment_act
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER physical_line_immutable BEFORE UPDATE OR DELETE ON wms.physical_shipment_line
FOR EACH ROW EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER physical_line_no_truncate BEFORE TRUNCATE ON wms.physical_shipment_line
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_reservation_change();

CREATE FUNCTION wms.protect_physical_movement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='TRUNCATE' THEN
   IF EXISTS (SELECT 1 FROM wms.physical_shipment_line) THEN RAISE EXCEPTION 'Linked physical movement immutable'; END IF;
 ELSIF EXISTS (SELECT 1 FROM wms.physical_shipment_line WHERE movement_id=OLD.id) THEN
   RAISE EXCEPTION 'Linked physical movement immutable';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER physical_movement_immutable BEFORE UPDATE OR DELETE ON wms.stock_movement
FOR EACH ROW EXECUTE FUNCTION wms.protect_physical_movement();
CREATE TRIGGER physical_movement_no_truncate BEFORE TRUNCATE ON wms.stock_movement
FOR EACH STATEMENT EXECUTE FUNCTION wms.protect_physical_movement();

CREATE FUNCTION wms.check_physical_package() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE k integer; p wms.physical_shipment_act%ROWTYPE; r wms.invoice_reservation%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='physical_shipment_act' THEN k:=NEW.id; ELSE k:=NEW.act_id; END IF;
 SELECT * INTO STRICT p FROM wms.physical_shipment_act WHERE id=k;
 SELECT * INTO STRICT r FROM wms.invoice_reservation WHERE document_id=p.document_id;
 IF p.organization_id<>r.organization_id OR p.reservation_digest<>r.digest
    OR p.document_version IS DISTINCT FROM (r.snapshot->>'version')::integer
    OR p.content_sha256 IS DISTINCT FROM r.snapshot->>'content_sha256'
    OR p.snapshot->>'kind' IS DISTINCT FROM 'internal_physical_shipment'
    OR (p.snapshot->>'document_id')::integer IS DISTINCT FROM p.document_id
    OR (p.snapshot->>'organization_id')::integer IS DISTINCT FROM p.organization_id
    OR p.snapshot->>'source_key' IS DISTINCT FROM p.source_key
    OR p.snapshot->>'request_hash' IS DISTINCT FROM p.request_hash
    OR p.snapshot->>'actor' IS DISTINCT FROM p.actor
    OR p.snapshot->>'operation_date' IS DISTINCT FROM p.operation_date::text
    OR jsonb_typeof(p.snapshot::jsonb->'lines') IS DISTINCT FROM 'array'
    OR jsonb_array_length(p.snapshot::jsonb->'lines')=0
    OR jsonb_array_length(p.snapshot::jsonb->'lines')<>(SELECT count(*) FROM wms.physical_shipment_line WHERE act_id=k)
    OR EXISTS (SELECT 1 FROM wms.invoice_reservation_release WHERE document_id=p.document_id)
 THEN RAISE EXCEPTION 'Invalid physical act identity/package'; END IF;
 IF EXISTS (
  SELECT 1 FROM wms.physical_shipment_line l
  JOIN wms.reservation_version b ON b.id=l.before_id
  JOIN wms.reservation_version a ON a.id=l.after_id
  JOIN wms.stock_movement m ON m.id=l.movement_id
  WHERE l.act_id=k AND (
   b.organization_id<>p.organization_id OR a.organization_id<>p.organization_id OR m.organization_id IS DISTINCT FROM p.organization_id
   OR b.source<>l.source OR a.source<>l.source OR b.sku_code<>l.sku_code OR a.sku_code<>l.sku_code OR m.sku_code<>l.sku_code
   OR b.warehouse<>l.warehouse OR a.warehouse<>l.warehouse OR m.warehouse<>l.warehouse
   OR a.version<>b.version+1 OR b.qty-l.qty<>a.qty OR a.qty<0
   OR m.kind<>'out' OR m.reason<>'shipment' OR m.qty<>l.qty OR m.doc_ref IS DISTINCT FROM 'physical-act:'||p.id::text
   OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r.snapshot::jsonb->'allocations') e
       WHERE e->>'source'=l.source AND (e->>'line_no')::integer=l.line_no AND e->>'sku_code'=l.sku_code AND e->>'warehouse'=l.warehouse)
   OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(p.snapshot::jsonb->'lines') e
       WHERE e->>'source'=l.source AND (e->>'line_no')::integer=l.line_no
       AND (e->>'before_id')::integer=b.id AND (e->>'after_id')::integer=a.id
       AND (e->>'movement_id')::integer=m.id AND (e->>'qty')::numeric=l.qty
       AND e->>'sku_code'=l.sku_code AND e->>'warehouse'=l.warehouse)
  )
 ) THEN RAISE EXCEPTION 'Invalid physical consumption links'; END IF;
 RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER physical_package AFTER INSERT ON wms.physical_shipment_act
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_physical_package();
CREATE CONSTRAINT TRIGGER physical_line_package AFTER INSERT ON wms.physical_shipment_line
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_physical_package();

-- Extend the existing deferred namespace protocol, retaining no-shipment release.
CREATE OR REPLACE FUNCTION wms.check_invoice_reservation_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owner_id integer; allocation jsonb;
BEGIN
 IF NEW.source NOT LIKE 'invoice:%' THEN RETURN NEW; END IF;
 IF NEW.source !~ '^invoice:[0-9]+:[0-9]+:[0-9a-f]{32}$' THEN RAISE EXCEPTION 'Invalid invoice namespace'; END IF;
 owner_id:=split_part(NEW.source,':',2)::integer;
 SELECT e INTO allocation FROM wms.invoice_reservation r,
 LATERAL jsonb_array_elements(r.snapshot::jsonb->'allocations') e
 WHERE r.document_id=owner_id AND r.organization_id=NEW.organization_id AND e->>'source'=NEW.source;
 IF allocation IS NULL OR allocation->>'sku_code'<>NEW.sku_code OR allocation->>'warehouse'<>NEW.warehouse
 OR (allocation->>'line_no')::integer<>split_part(NEW.source,':',3)::integer THEN RAISE EXCEPTION 'Invalid invoice identity'; END IF;
 IF NEW.version=1 AND NEW.qty=(allocation->>'qty')::numeric AND NEW.qty>0 THEN RETURN NEW; END IF;
 IF EXISTS (SELECT 1 FROM wms.physical_shipment_line l JOIN wms.physical_shipment_act p ON p.id=l.act_id
  WHERE l.after_id=NEW.id AND l.source=NEW.source AND p.document_id=owner_id AND p.organization_id=NEW.organization_id)
 THEN RETURN NEW; END IF;
 IF NEW.version=2 AND NEW.qty=0 AND NOT EXISTS (SELECT 1 FROM wms.physical_shipment_act WHERE document_id=owner_id)
 AND EXISTS (SELECT 1 FROM wms.invoice_reservation_release_line l JOIN wms.invoice_reservation_release p ON p.id=l.release_id
  WHERE l.after_id=NEW.id AND l.source=NEW.source AND p.organization_id=NEW.organization_id AND p.document_id=owner_id)
 THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'Invoice quantity change requires linked evidence';
END $$;

CREATE FUNCTION wms.no_release_after_physical() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF EXISTS (SELECT 1 FROM wms.physical_shipment_act WHERE document_id=NEW.document_id) THEN
  RAISE EXCEPTION 'Physical act excludes no-shipment release';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER no_release_after_physical BEFORE INSERT ON wms.invoice_reservation_release
FOR EACH ROW EXECUTE FUNCTION wms.no_release_after_physical();
