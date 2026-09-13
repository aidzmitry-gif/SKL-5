CREATE OR REPLACE FUNCTION wms.reject_reservation_change() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'Reservation versions are immutable';
END $$;
CREATE TRIGGER reservation_version_immutable BEFORE UPDATE OR DELETE ON wms.reservation_version
FOR EACH ROW EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER reservation_version_no_truncate BEFORE TRUNCATE ON wms.reservation_version
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER invoice_reservation_immutable BEFORE UPDATE OR DELETE ON wms.invoice_reservation
FOR EACH ROW EXECUTE FUNCTION wms.reject_reservation_change();
CREATE TRIGGER invoice_reservation_no_truncate BEFORE TRUNCATE ON wms.invoice_reservation
FOR EACH STATEMENT EXECUTE FUNCTION wms.reject_reservation_change();
