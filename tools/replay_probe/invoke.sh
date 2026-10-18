#!/usr/bin/env bash
set -euo pipefail

probe_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$probe_dir/../.." && pwd)"
tool_cache="$repo_root/local-data/tool-cache/replay-probe"
gradle_version=8.14.4
gradle_dir="$tool_cache/gradle-$gradle_version"
gradle_sha256=f1771298a70f6db5a29daf62378c4e18a17fc33c9ba6b14362e0cdf40610380d
jdk_version=17.0.20.1_1
jdk_release='jdk-17.0.20.1%2B1'
pending_file=''
pending_dir=''

die() { printf '%s\n' "$*" >&2; exit 1; }
cleanup() {
    if [[ -n "$pending_file" ]]; then rm -f -- "$pending_file"; fi
    if [[ -n "$pending_dir" ]]; then rm -rf -- "$pending_dir"; fi
}
trap cleanup EXIT

usage() {
    cat <<'USAGE'
Usage: tools/replay_probe/invoke.sh --prepare
       tools/replay_probe/invoke.sh check|probeTest|--version
       tools/replay_probe/invoke.sh probeReplay|probeCompactReplay REPLAY OUTPUT

Only --prepare downloads tools and dependencies. Other commands run offline.
Relative replay/output paths are resolved from the repository root.
JAVA_HOME may select an existing JDK 17 instead of the local pinned Temurin JDK.
USAGE
}
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then usage; exit 0; fi

case "$(uname -s)" in
    Darwin) jdk_os=mac ;;
    Linux) jdk_os=linux ;;
    *) die 'Supported operating systems: macOS and Linux.' ;;
esac
case "$(uname -m)" in
    arm64|aarch64) jdk_arch=aarch64 ;;
    x86_64|amd64) jdk_arch=x64 ;;
    *) die 'Supported architectures: arm64/aarch64 and x86_64.' ;;
esac
case "$jdk_os-$jdk_arch" in
    mac-aarch64) jdk_sha256=196d13ba5f10414bef7f6a05a9b3f00edacb18ebacef2b99485db9e2ee18f0e8 ;;
    mac-x64) jdk_sha256=c01975da12ed4235250ff891fe8bba73a9e73037d444b269c9d0922b5dbc8e0a ;;
    linux-aarch64) jdk_sha256=457b57af8f9c93ec39080bb8c764f559dc8c89a6da1a39d718a400b7890d3e41 ;;
    linux-x64) jdk_sha256=3808d1d15e3ec6bd5b84057fb5d84c33d8a1536a258146bcea2e603fc726e08e ;;
esac
jdk_dir="$tool_cache/temurin-$jdk_version-$jdk_os-$jdk_arch"
cached_java_home="$jdk_dir"
if [[ "$jdk_os" == mac ]]; then cached_java_home="$jdk_dir/Contents/Home"; fi
selected_java_home="${JAVA_HOME:-$cached_java_home}"

is_jdk17() {
    [[ -x "$1/bin/java" && -x "$1/bin/javac" ]] || return 1
    case "$("$1/bin/javac" -version 2>&1)" in
        'javac 17'|'javac 17.'*|'javac 17+'*) return 0 ;;
        *) return 1 ;;
    esac
}
verify_sha256() {
    local actual
    if command -v shasum >/dev/null 2>&1; then
        actual="$(shasum -a 256 "$1")"
    elif command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$1")"
    else
        die 'SHA-256 verification requires shasum or sha256sum.'
    fi
    [[ "${actual%% *}" == "$2" ]]
}
download_verified() {
    local url="$1" checksum="$2" destination="$3"
    if [[ -f "$destination" ]]; then
        verify_sha256 "$destination" "$checksum" || die "Checksum mismatch: $destination"
        return
    fi
    mkdir -p "$(dirname "$destination")"
    pending_file="$(mktemp "$destination.download.XXXXXX")"
    printf 'Downloading %s\n' "${destination##*/}"
    curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 900 --output "$pending_file" "$url"
    verify_sha256 "$pending_file" "$checksum" || die "Checksum mismatch: $destination"
    mv -- "$pending_file" "$destination"
    pending_file=''
}
jar_specs() {
    # Maven Central coordinates; all artifacts are pinned by SHA-256.
    cat <<'JARS'
445c129ddadf2906972108d82aa73b6e152b21ed3bfe1ed62612ec6f39ea9de8 com/skadistats/clarity/4.0.1/clarity-4.0.1.jar
021c45dbad3fe46dc99b3753db2f68df96fdc6cafbc7386e46684df5aca149ce com/skadistats/clarity-protobuf/6.1/clarity-protobuf-6.1.jar
f31c20f5b06312f3d5e06e6160a32e274d819aa6cebf27528b26b6b5c0c1df19 it/unimi/dsi/fastutil-core/8.5.12/fastutil-core-8.5.12.jar
55b30c94e5c4cc2d4b6976916098d0678a8a6cc7427fa8c875621bd94e731ac8 org/xerial/snappy/snappy-java/1.1.10.4/snappy-java-1.1.10.4.jar
5d6298b93a1905c32cda6478808ac14c2d4a47e91535e53c41f7feeb85d946f4 org/slf4j/slf4j-api/2.0.7/slf4j-api-2.0.7.jar
JARS
}

task="${1:-check}"
case "$task" in
    --prepare)
        [[ $# == 1 ]] || die 'Use --prepare without additional arguments.'
        for tool in curl tar unzip; do command -v "$tool" >/dev/null || die "Missing tool: $tool"; done
        mkdir -p "$tool_cache"
        if ! is_jdk17 "$selected_java_home"; then
            [[ -z "${JAVA_HOME:-}" ]] || die 'JAVA_HOME must point to a JDK 17 (including javac).'
            [[ ! -e "$jdk_dir" ]] || die "Invalid cached JDK: $jdk_dir"
            jdk_archive="$tool_cache/OpenJDK17U-jdk_${jdk_arch}_${jdk_os}_hotspot_${jdk_version}.tar.gz"
            download_verified "https://github.com/adoptium/temurin17-binaries/releases/download/$jdk_release/${jdk_archive##*/}" "$jdk_sha256" "$jdk_archive"
            pending_dir="$(mktemp -d "$tool_cache/.jdk.XXXXXX")"
            tar -xzf "$jdk_archive" -C "$pending_dir" --strip-components=1
            mv -- "$pending_dir" "$jdk_dir"
            pending_dir=''
            is_jdk17 "$selected_java_home" || die 'The downloaded JDK did not provide javac 17.'
        fi
        if [[ ! -x "$gradle_dir/bin/gradle" ]]; then
            [[ ! -e "$gradle_dir" ]] || die "Invalid cached Gradle: $gradle_dir"
            gradle_archive="$tool_cache/gradle-$gradle_version-bin.zip"
            download_verified "https://downloads.gradle.org/distributions/${gradle_archive##*/}" "$gradle_sha256" "$gradle_archive"
            pending_dir="$(mktemp -d "$tool_cache/.gradle.XXXXXX")"
            unzip -q "$gradle_archive" -d "$pending_dir"
            mv -- "$pending_dir/gradle-$gradle_version" "$gradle_dir"
            rmdir "$pending_dir"
            pending_dir=''
        fi
        while read -r checksum coordinate; do
            download_verified "https://repo.maven.apache.org/maven2/$coordinate" "$checksum" "$probe_dir/local-libs/${coordinate##*/}"
        done < <(jar_specs)
        printf 'Prepared JDK 17, Gradle %s and five verified JARs.\n' "$gradle_version"
        exit 0
        ;;
    check|probeTest|--version)
        [[ $# -le 1 ]] || die "$task does not accept replay/output arguments."
        task_args=("$task")
        ;;
    probeReplay|probeCompactReplay)
        [[ $# == 3 ]] || { usage >&2; exit 1; }
        case "$2" in /*) replay_path="$2" ;; *) replay_path="$repo_root/$2" ;; esac
        case "$3" in /*) output_path="$3" ;; *) output_path="$repo_root/$3" ;; esac
        [[ -f "$replay_path" ]] || die "Replay not found: $replay_path"
        task_args=("$task" "-Preplay=$replay_path" "-Poutput=$output_path")
        ;;
    *) usage >&2; die "Unknown task: $task" ;;
esac

is_jdk17 "$selected_java_home" || die 'JDK 17 is missing; run invoke.sh --prepare or set JAVA_HOME.'
[[ -x "$gradle_dir/bin/gradle" ]] || die 'Gradle 8.14.4 is missing; run invoke.sh --prepare.'
while read -r checksum coordinate; do
    artifact="$probe_dir/local-libs/${coordinate##*/}"
    [[ -f "$artifact" ]] || die "Missing $artifact; run invoke.sh --prepare."
    verify_sha256 "$artifact" "$checksum" || die "Checksum mismatch: $artifact"
done < <(jar_specs)
export JAVA_HOME="$selected_java_home"
export PATH="$JAVA_HOME/bin:$PATH"
export GRADLE_USER_HOME="$tool_cache/gradle-user-home"
cd "$repo_root"
exec "$gradle_dir/bin/gradle" --offline --no-daemon --console=plain -p "$probe_dir" \
    -Porg.gradle.java.installations.auto-download=false \
    -Porg.gradle.java.installations.auto-detect=false \
    "-Porg.gradle.java.installations.paths=$JAVA_HOME" "${task_args[@]}"
