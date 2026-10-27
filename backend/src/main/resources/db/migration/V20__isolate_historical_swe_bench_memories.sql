-- 已导入的经验来自已解决 SWE-bench 实例。隔离到专用作用域，
-- MemoryService 只会为非 SWE-bench 仓库任务检索该作用域。
UPDATE semantic_memory memory
JOIN memory_revision revision ON revision.memory_id = memory.id
SET memory.scope_key = 'HISTORICAL_SWE_BENCH'
WHERE revision.source_observation_id IS NULL
  AND revision.change_reason LIKE 'Imported verified SWE-bench Lite legacy memory from %';
