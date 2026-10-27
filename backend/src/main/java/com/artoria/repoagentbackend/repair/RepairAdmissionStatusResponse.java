package com.artoria.repoagentbackend.repair;

import java.time.Instant;

/** 面向提交端的准入与等待信息；系统不保留被拒绝的用户等待任务。 */
public record RepairAdmissionStatusResponse(
        int activeWorkflows,
        int maxActiveWorkflows,
        int remainingActiveSlots,
        int acceptedToday,
        int dailyTaskLimit,
        int remainingDailyQuota,
        int globalActiveWorkflows,
        int globalMaxInflight,
        long waitingStages,
        long estimatedStartWaitSeconds,
        Instant dailyQuotaResetsAt
) { }
