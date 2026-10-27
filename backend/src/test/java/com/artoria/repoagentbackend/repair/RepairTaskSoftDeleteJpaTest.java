package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.identity.AppUser;
import com.artoria.repoagentbackend.identity.AppUserJpaRepository;
import com.artoria.repoagentbackend.issue.RepositoryIssue;
import com.artoria.repoagentbackend.issue.RepositoryIssueJpaRepository;
import com.artoria.repoagentbackend.repository.CodeRepository;
import com.artoria.repoagentbackend.repository.CodeRepositoryJpaRepository;
import com.artoria.repoagentbackend.repository.RepositoryMember;
import com.artoria.repoagentbackend.repository.RepositoryMemberJpaRepository;
import com.artoria.repoagentbackend.repository.RepositoryRole;
import jakarta.persistence.EntityManager;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest;

import static org.assertj.core.api.Assertions.assertThat;

@DataJpaTest(properties = {
        "spring.flyway.enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop",
        "spring.datasource.url=jdbc:h2:mem:repair-delete-test;MODE=MySQL;DB_CLOSE_DELAY=-1"
})
class RepairTaskSoftDeleteJpaTest {
    @Autowired AppUserJpaRepository users;
    @Autowired CodeRepositoryJpaRepository repositories;
    @Autowired RepositoryMemberJpaRepository members;
    @Autowired RepositoryIssueJpaRepository issues;
    @Autowired RepairTaskJpaRepository tasks;
    @Autowired EntityManager entityManager;

    @Test
    void deletedTaskIsExcludedFromQueriesAndStatistics() {
        AppUser owner = users.save(new AppUser(1001L, "owner", "Owner"));
        CodeRepository repository = repositories.save(CodeRepository.sweBench("pallets/flask", owner));
        members.save(new RepositoryMember(repository, owner, RepositoryRole.OWNER));
        RepositoryIssue issue = issues.save(RepositoryIssue.sweBench(
                repository, "pallets__flask-4045", "Issue", "Body", "lite", "base-sha"
        ));
        RepairTask task = new RepairTask(issue, "base-sha", "repoagent/test-delete", "request-delete");
        task.transitionTo(RepairTaskStatus.QUEUED);
        task = tasks.saveAndFlush(task);
        assertThat(tasks.findAccessibleForStatistics(owner.getId(), null, null)).hasSize(1);

        task.markDeleted();
        tasks.flush();
        entityManager.clear();

        assertThat(tasks.findById(task.getId())).isEmpty();
        assertThat(tasks.findAccessibleForStatistics(owner.getId(), null, null)).isEmpty();
    }
}
