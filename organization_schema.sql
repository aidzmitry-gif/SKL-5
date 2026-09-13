-- Additive proposal only. Legacy ownership remains NULL pending reconciliation.
ALTER TABLE wms.receipt ADD COLUMN organization_id INTEGER;
ALTER TABLE wms.receipt ADD COLUMN source_event_id INTEGER;
ALTER TABLE wms.receipt ADD CONSTRAINT receipt_source_event_id_key UNIQUE (source_event_id);
ALTER TABLE wms.stock_movement ADD COLUMN organization_id INTEGER;
ALTER TABLE wms.task ADD COLUMN organization_id INTEGER;
ALTER TABLE wms.receipt ADD CONSTRAINT wms_receipt_org_positive CHECK (organization_id > 0);
ALTER TABLE wms.stock_movement ADD CONSTRAINT wms_movement_org_positive CHECK (organization_id > 0);
ALTER TABLE wms.task ADD CONSTRAINT wms_task_org_positive CHECK (organization_id > 0);

-- Unallocated additive proposal: no migration revision and no ownership backfill.
ALTER TABLE wms.inventory_count ADD COLUMN organization_id INTEGER;
ALTER TABLE wms.cycle_count_plan ADD COLUMN organization_id INTEGER;
ALTER TABLE wms.inventory_count ADD CONSTRAINT wms_inventory_count_org_positive CHECK (organization_id > 0);
ALTER TABLE wms.cycle_count_plan ADD CONSTRAINT wms_cycle_count_plan_org_positive CHECK (organization_id > 0);
ALTER TABLE wms.inventory_count ADD COLUMN expected_source VARCHAR(32);
ALTER TABLE wms.inventory_count ADD COLUMN source_evidence VARCHAR(1000);
ALTER TABLE wms.inventory_count ADD COLUMN journal_confirmed_by VARCHAR(200);
ALTER TABLE wms.inventory_count ADD COLUMN journal_confirmed_at TIMESTAMP;
ALTER TABLE wms.cycle_count_plan ADD COLUMN expected_source VARCHAR(32);
ALTER TABLE wms.cycle_count_plan ADD COLUMN source_evidence VARCHAR(1000);
ALTER TABLE wms.cycle_count_plan ADD COLUMN journal_confirmed_by VARCHAR(200);
ALTER TABLE wms.cycle_count_plan ADD COLUMN journal_confirmed_at TIMESTAMP;
ALTER TABLE wms.inventory_count ADD COLUMN snapshot_version VARCHAR(64);
ALTER TABLE wms.inventory_count ADD COLUMN snapshot_cutoff INTEGER;
ALTER TABLE wms.inventory_count ADD COLUMN snapshot_at TIMESTAMP;
