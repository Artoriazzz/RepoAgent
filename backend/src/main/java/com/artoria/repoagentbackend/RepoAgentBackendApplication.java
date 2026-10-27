package com.artoria.repoagentbackend;

import com.artoria.repoagentbackend.repair.RepairAdmissionProperties;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.data.web.config.EnableSpringDataWebSupport;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableSpringDataWebSupport(pageSerializationMode = EnableSpringDataWebSupport.PageSerializationMode.VIA_DTO)
@EnableScheduling
@EnableConfigurationProperties(RepairAdmissionProperties.class)
public class RepoAgentBackendApplication {

    public static void main(String[] args) {
        SpringApplication.run(RepoAgentBackendApplication.class, args);
    }
}
