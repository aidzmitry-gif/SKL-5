-- Apply after the Accounting physical-shipment proposal, in the same release.
CREATE TRIGGER remainder_immutable BEFORE UPDATE OR DELETE ON wms.invoice_remainder_release
FOR EACH ROW EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER remainder_no_truncate BEFORE TRUNCATE ON wms.invoice_remainder_release
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER remainder_line_immutable BEFORE UPDATE OR DELETE ON wms.invoice_remainder_release_line
FOR EACH ROW EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER remainder_line_no_truncate BEFORE TRUNCATE ON wms.invoice_remainder_release_line
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_reservation_change();

CREATE FUNCTION wms.check_remainder_package() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE k integer; p wms.invoice_remainder_release%ROWTYPE; r wms.invoice_reservation%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='invoice_remainder_release' THEN k:=NEW.id; ELSE k:=NEW.release_id; END IF;
 SELECT * INTO STRICT p FROM wms.invoice_remainder_release WHERE id=k;
 SELECT * INTO STRICT r FROM wms.invoice_reservation WHERE document_id=p.document_id;
 IF p.organization_id IS DISTINCT FROM r.organization_id
 OR p.snapshot->>'reservation_digest' IS DISTINCT FROM r.digest
 OR (p.snapshot->>'organization_id')::integer IS DISTINCT FROM p.organization_id
 OR (p.snapshot->>'document_id')::integer IS DISTINCT FROM p.document_id
 OR p.snapshot->>'source_key' IS DISTINCT FROM p.source_key
 OR p.snapshot->>'request_hash' IS DISTINCT FROM p.request_hash
 OR p.snapshot->>'actor' IS DISTINCT FROM p.actor
 OR jsonb_typeof(p.snapshot::jsonb->'lines') IS DISTINCT FROM 'array'
 OR jsonb_array_length(p.snapshot::jsonb->'lines')=0
 OR jsonb_array_length(p.snapshot::jsonb->'lines')<>(SELECT count(*) FROM wms.invoice_remainder_release_line WHERE release_id=k)
 OR NOT EXISTS(SELECT 1 FROM wms.physical_shipment_act WHERE document_id=p.document_id)
 OR EXISTS(SELECT 1 FROM wms.invoice_reservation_release WHERE document_id=p.document_id)
 THEN RAISE EXCEPTION 'Invalid remainder identity/package'; END IF;
 IF EXISTS (
   SELECT 1 FROM wms.invoice_remainder_release_line l
   JOIN wms.reservation_version b ON b.id=l.before_id
   JOIN wms.reservation_version a ON a.id=l.after_id
   WHERE l.release_id=k AND (
     b.organization_id<>p.organization_id OR a.organization_id<>p.organization_id
     OR b.source<>l.source OR a.source<>l.source OR b.sku_code<>a.sku_code OR b.warehouse<>a.warehouse
     OR b.qty<=0 OR l.qty<>b.qty OR a.qty<>0 OR a.version<>b.version+1
     OR EXISTS(SELECT 1 FROM wms.physical_shipment_line WHERE before_id=b.id OR after_id=a.id)
     OR EXISTS(SELECT 1 FROM wms.reservation_version v WHERE v.source=a.source AND v.version>a.version)
     OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(r.snapshot::jsonb->'allocations') e
       WHERE e->>'source'=l.source AND (e->>'line_no')::integer=l.line_no
         AND e->>'sku_code'=b.sku_code AND e->>'warehouse'=b.warehouse)
     OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.snapshot::jsonb->'lines') e
       WHERE e->>'source'=l.source AND (e->>'line_no')::integer=l.line_no
         AND (e->>'before_id')::integer=b.id AND (e->>'after_id')::integer=a.id AND (e->>'qty')::numeric=l.qty)
   )
 ) THEN RAISE EXCEPTION 'Invalid remainder version links'; END IF;
 -- Releasing the remainder closes every allocation, including already exhausted lines.
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(r.snapshot::jsonb->'allocations') e
   WHERE (SELECT qty FROM wms.reservation_version v WHERE v.source=e->>'source'
     AND v.organization_id=p.organization_id ORDER BY version DESC LIMIT 1) IS DISTINCT FROM 0::numeric)
 THEN RAISE EXCEPTION 'Incomplete remainder release'; END IF;
 RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER remainder_package AFTER INSERT ON wms.invoice_remainder_release
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_remainder_package();
CREATE CONSTRAINT TRIGGER remainder_line_package AFTER INSERT ON wms.invoice_remainder_release_line
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.check_remainder_package();

CREATE FUNCTION wms.no_physical_after_remainder() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF EXISTS(SELECT 1 FROM wms.invoice_remainder_release WHERE document_id=NEW.document_id)
 THEN RAISE EXCEPTION 'Reservation remainder is closed'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER no_physical_after_remainder BEFORE INSERT ON wms.physical_shipment_act
FOR EACH ROW EXECUTE FUNCTION wms.no_physical_after_remainder();

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
 IF EXISTS(SELECT 1 FROM wms.invoice_remainder_release_line l JOIN wms.invoice_remainder_release p ON p.id=l.release_id
   WHERE l.after_id=NEW.id AND l.source=NEW.source AND p.document_id=owner_id AND p.organization_id=NEW.organization_id)
 THEN RETURN NEW; END IF;
 IF EXISTS(SELECT 1 FROM wms.invoice_remainder_release WHERE document_id=owner_id)
 THEN RAISE EXCEPTION 'Reservation remainder is closed'; END IF;
 IF EXISTS(SELECT 1 FROM wms.physical_shipment_line l JOIN wms.physical_shipment_act p ON p.id=l.act_id
   WHERE l.after_id=NEW.id AND l.source=NEW.source AND p.document_id=owner_id AND p.organization_id=NEW.organization_id)
 THEN RETURN NEW; END IF;
 IF NEW.version=2 AND NEW.qty=0 AND NOT EXISTS(SELECT 1 FROM wms.physical_shipment_act WHERE document_id=owner_id)
 AND EXISTS(SELECT 1 FROM wms.invoice_reservation_release_line l JOIN wms.invoice_reservation_release p ON p.id=l.release_id
   WHERE l.after_id=NEW.id AND l.source=NEW.source AND p.organization_id=NEW.organization_id AND p.document_id=owner_id)
 THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'Invoice quantity change requires linked evidence';
END $$;
