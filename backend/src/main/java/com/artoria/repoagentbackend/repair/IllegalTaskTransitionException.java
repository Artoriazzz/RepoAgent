package com.artoria.repoagentbackend.repair;

public class IllegalTaskTransitionException extends RuntimeException {
    public IllegalTaskTransitionException(RepairTaskStatus from, RepairTaskStatus to) {
        super("Repair task cannot transition from " + from + " to " + to);
    }
}
