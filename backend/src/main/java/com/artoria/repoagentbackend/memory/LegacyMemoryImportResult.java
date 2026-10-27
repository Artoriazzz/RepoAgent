package com.artoria.repoagentbackend.memory;

/** 一次历史记忆导入的汇总，供启动日志和运维脚本确认结果。 */
public record LegacyMemoryImportResult(int scanned, int imported, int existing, int skipped) {
}
