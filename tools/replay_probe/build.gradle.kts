plugins {
    java
    application
}

group = "local.dota"
version = "1.0-SNAPSHOT"

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(17))
    }
}

val localClarityClasspath = files(listOf(
    "clarity-4.0.1.jar",
    "clarity-protobuf-6.1.jar",
    "fastutil-core-8.5.12.jar",
    "snappy-java-1.1.10.4.jar",
    "slf4j-api-2.0.7.jar",
).map {
    val artifact = file("local-libs/$it")
    if (!artifact.isFile) {
        throw GradleException("Missing $artifact; invoke this build through invoke.ps1")
    }
    artifact
})

dependencies {
    implementation(localClarityClasspath)
    annotationProcessor(localClarityClasspath)
    testAnnotationProcessor(localClarityClasspath)
}

application {
    mainClass.set("local.dota.replayprobe.ReplayProbe")
}

tasks.register<JavaExec>("probeTest") {
    group = "verification"
    description = "Runs the dependency-free focused probe tests."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass.set("local.dota.replayprobe.ProbeSelfTest")
}

tasks.named("check") {
    dependsOn("probeTest")
}

tasks.register<JavaExec>("probeReplay") {
    group = "application"
    description = "Parses one local replay and writes the feasibility JSON."
    dependsOn(tasks.classes)
    classpath = sourceSets.main.get().runtimeClasspath
    mainClass.set("local.dota.replayprobe.ReplayProbe")
    maxHeapSize = "4g"

    doFirst {
        val replay = providers.gradleProperty("replay").orNull
            ?: throw GradleException("Pass -Preplay=<path-to-dem>")
        val output = providers.gradleProperty("output").orNull
            ?: throw GradleException("Pass -Poutput=<path-to-json>")
        args(replay, output)
    }
}
