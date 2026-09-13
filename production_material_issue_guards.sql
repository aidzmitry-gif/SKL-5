CREATE FUNCTION wms.reject_production_material_issue_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Production material issue binding is immutable';
END $$;
CREATE TRIGGER immutable_production_material_issue
BEFORE UPDATE OR DELETE OR TRUNCATE ON wms.production_material_issue
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_production_material_issue_mutation();

CREATE FUNCTION wms.check_production_material_issue_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE own production.order_ownership%ROWTYPE; m wms.stock_movement%ROWTYPE; cmd jsonb; snap jsonb;
BEGIN
  SELECT * INTO own FROM production.order_ownership
  WHERE order_id=NEW.order_id FOR UPDATE;
  IF NOT FOUND OR own.organization_id IS DISTINCT FROM NEW.organization_id THEN
    RAISE EXCEPTION 'Production material issue order ownership mismatch';
  END IF;
  IF NEW.request_key !~ '^[0-9a-fA-F-]{36}$' THEN
    RAISE EXCEPTION 'Production material issue request key is not a UUID';
  END IF;
  cmd := to_jsonb(NEW.snapshot)->'command';
  snap := to_jsonb(NEW.snapshot)->'order_snapshot';
  IF cmd IS NULL OR snap IS NULL
    OR cmd->>'request_id' IS DISTINCT FROM NEW.request_key
    OR cmd->>'order_id' IS DISTINCT FROM NEW.order_id::text
    OR cmd->>'expected_order_digest' IS DISTINCT FROM own.digest
    OR cmd->>'operation_date' IS DISTINCT FROM NEW.operation_date::text
    OR snap IS DISTINCT FROM to_jsonb(own.snapshot) THEN
    RAISE EXCEPTION 'Production material issue command snapshot mismatch';
  END IF;
  SELECT * INTO m FROM wms.stock_movement WHERE id=NEW.movement_id;
  IF NOT FOUND OR m.organization_id IS DISTINCT FROM NEW.organization_id
    OR ROW(m.sku_code,m.warehouse,m.kind,m.qty,m.reason,m.location_id,m.batch_ref,m.doc_ref,m.note)
      IS DISTINCT FROM ROW(cmd->>'sku_code',cmd->>'warehouse','out',(cmd->>'quantity')::numeric,
                           'production_issue',NULLIF(cmd->>'location_id','')::integer,cmd->>'lot',
                           'production_material:'||NEW.request_key,cmd->>'evidence') THEN
    RAISE EXCEPTION 'Production material issue movement mismatch';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER check_production_material_issue_insert
BEFORE INSERT ON wms.production_material_issue
FOR EACH ROW EXECUTE FUNCTION wms.check_production_material_issue_insert();

CREATE FUNCTION wms.protect_production_material_issue_movement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='TRUNCATE' THEN
    IF EXISTS (SELECT 1 FROM wms.production_material_issue) THEN
      RAISE EXCEPTION 'Production material issue movement is immutable';
    END IF;
  ELSIF EXISTS (SELECT 1 FROM wms.production_material_issue WHERE movement_id=OLD.id) THEN
    RAISE EXCEPTION 'Production material issue movement is immutable';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER protect_production_material_issue_movement
BEFORE UPDATE OR DELETE ON wms.stock_movement
FOR EACH ROW EXECUTE FUNCTION wms.protect_production_material_issue_movement();
CREATE TRIGGER protect_production_material_issue_movement_truncate
BEFORE TRUNCATE ON wms.stock_movement
FOR EACH STATEMENT EXECUTE FUNCTION wms.protect_production_material_issue_movement();

CREATE FUNCTION wms.check_production_material_issue_state(target_id integer) RETURNS void LANGUAGE plpgsql AS $$
DECLARE m wms.stock_movement%ROWTYPE;
BEGIN
  SELECT * INTO m FROM wms.stock_movement WHERE id=target_id;
  IF NOT FOUND OR m.reason <> 'production_issue' OR m.doc_ref NOT LIKE 'production_material:%' THEN RETURN; END IF;
  IF NOT EXISTS (SELECT 1 FROM wms.production_material_issue WHERE movement_id=m.id) THEN
    RAISE EXCEPTION 'Production material movement requires its immutable source binding';
  END IF;
END $$;

CREATE FUNCTION wms.require_production_material_issue_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' THEN
    IF OLD.reason='production_issue' AND OLD.doc_ref LIKE 'production_material:%'
      AND EXISTS (SELECT 1 FROM wms.production_material_issue WHERE movement_id=OLD.id) THEN
      RAISE EXCEPTION 'Production material movement is bound and cannot be deleted';
    END IF;
  ELSE
    PERFORM wms.check_production_material_issue_state(NEW.id);
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER require_production_material_issue_binding
AFTER INSERT OR UPDATE OR DELETE ON wms.stock_movement
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wms.require_production_material_issue_binding();
