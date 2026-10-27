package com.artoria.repoagentbackend.repair;

import org.springframework.boot.context.properties.ConfigurationProperties;

/** 新工作流入口的容量治理参数。 */
@ConfigurationProperties(prefix = "app.repair.admission")
public class RepairAdmissionProperties {
    private int defaultMaxActiveWorkflows = 3;
    private int defaultDailyTaskLimit = 20;
    private int globalMaxInflight = 100;
    private int globalMaxWaitingStages = 200;
    private int outboxBacklogLimit = 500;
    private int requestsPerMinute = 30;

    public int getDefaultMaxActiveWorkflows() { return defaultMaxActiveWorkflows; }
    public void setDefaultMaxActiveWorkflows(int value) { this.defaultMaxActiveWorkflows = value; }
    public int getDefaultDailyTaskLimit() { return defaultDailyTaskLimit; }
    public void setDefaultDailyTaskLimit(int value) { this.defaultDailyTaskLimit = value; }
    public int getGlobalMaxInflight() { return globalMaxInflight; }
    public void setGlobalMaxInflight(int value) { this.globalMaxInflight = value; }
    public int getGlobalMaxWaitingStages() { return globalMaxWaitingStages; }
    public void setGlobalMaxWaitingStages(int value) { this.globalMaxWaitingStages = value; }
    public int getOutboxBacklogLimit() { return outboxBacklogLimit; }
    public void setOutboxBacklogLimit(int value) { this.outboxBacklogLimit = value; }
    public int getRequestsPerMinute() { return requestsPerMinute; }
    public void setRequestsPerMinute(int value) { this.requestsPerMinute = value; }
}
