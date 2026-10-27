package com.artoria.repoagentbackend.repair;

import org.springframework.stereotype.Component;

import java.util.UUID;

@Component
public class RepairBranchNameFactory {
    public String create(int issueNumber, UUID taskId) {
        return "repoagent/issue-" + issueNumber + "/run-" + taskId;
    }

    public String create(String externalIssueId, UUID taskId) {
        String safeId = externalIssueId.replaceAll("[^A-Za-z0-9._-]", "-");
        return "repoagent/benchmark-" + safeId + "/run-" + taskId;
    }
}
