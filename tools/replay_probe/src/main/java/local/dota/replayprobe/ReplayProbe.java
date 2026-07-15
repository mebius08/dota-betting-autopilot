package local.dota.replayprobe;

import com.google.protobuf.ByteString;
import com.google.protobuf.Descriptors;
import com.google.protobuf.Message;
import skadistats.clarity.Clarity;
import skadistats.clarity.event.Insert;
import skadistats.clarity.io.Util;
import skadistats.clarity.model.Entity;
import skadistats.clarity.model.FieldPath;
import skadistats.clarity.processor.entities.Entities;
import skadistats.clarity.processor.entities.UsesEntities;
import skadistats.clarity.processor.reader.OnTickEnd;
import skadistats.clarity.processor.runner.Context;
import skadistats.clarity.processor.runner.SimpleRunner;
import skadistats.clarity.source.MappedFileSource;
import skadistats.clarity.wire.dota.s2.proto.DOTAS2MatchMetadata;
import skadistats.clarity.wire.shared.demo.proto.Demo;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeSet;

@UsesEntities
public final class ReplayProbe {
    static final String GAME_RULES_ENTITY = "CDOTAGamerulesProxy";
    static final String GAME_TIME_PATH = "m_pGameRules.m_fGameTime";
    static final List<String> GAME_TIME_PATHS = List.of(
            GAME_TIME_PATH,
            "m_pGameRules.m_flGameTime"
    );
    static final String GAME_STATE_PATH = "m_pGameRules.m_nGameState";
    static final String GAME_PAUSED_PATH = "m_pGameRules.m_bGamePaused";
    static final String TOTAL_PAUSED_TICKS_PATH = "m_pGameRules.m_nTotalPausedTicks";
    static final String PAUSE_START_TICK_PATH = "m_pGameRules.m_nPauseStartTick";
    static final String GAME_START_TIME_PATH = "m_pGameRules.m_flGameStartTime";
    static final String GAME_END_TIME_PATH = "m_pGameRules.m_flGameEndTime";
    static final int SNAPSHOT_INTERVAL_SECONDS = 60;
    private static final List<String> REQUESTED_PLAYER_FIELDS = List.of(
            "player_slot", "team", "hero_id", "hero_name", "level", "kills", "deaths", "assists",
            "last_hits", "denies", "net_worth", "current_gold", "total_xp", "items"
    );
    private static final List<String> FINAL_COMPARISON_FIELDS = List.of(
            "kills", "deaths", "assists", "level", "last_hits", "denies", "net_worth"
    );

    private final Path replayPath;
    private final Path outputPath;
    private final ClockTracker clock = new ClockTracker();
    private final List<Map<String, Object>> snapshots = new ArrayList<>();
    private final Map<String, PropertyProvenance> fieldProvenance = new LinkedHashMap<>();
    private int nextSnapshotSecond;
    private int lastReplayTick;
    private double lastRawReplayTime;
    private Float lastGameTime;
    private Float lastDirectGameTime;
    private Integer lastGameState;
    private String actualGameTimePath;

    @Insert
    private Entities entities;

    ReplayProbe(Path replayPath, Path outputPath) {
        this.replayPath = replayPath;
        this.outputPath = outputPath;
        for (String field : REQUESTED_PLAYER_FIELDS) {
            fieldProvenance.put(field, new PropertyProvenance());
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            System.err.println("Usage: ReplayProbe <replay.dem> <output.json>");
            System.exit(2);
        }
        Path replay = Path.of(args[0]).toAbsolutePath().normalize();
        Path output = Path.of(args[1]).toAbsolutePath().normalize();
        if (!Files.isRegularFile(replay)) {
            throw new IllegalArgumentException("Replay not found: " + replay);
        }

        ReplayProbe probe = new ReplayProbe(replay, output);
        Map<String, Object> result = probe.run();
        Path parent = output.getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        Files.writeString(output, JsonWriter.toJson(result), StandardCharsets.UTF_8);

        @SuppressWarnings("unchecked")
        Map<String, Object> validation = (Map<String, Object>) result.get("validation");
        System.out.println("wrote " + output);
        System.out.println("clock_normalization=" + (probe.clock.isProven() ? "PROVEN" : "UNRESOLVED"));
        System.out.println("player_count=" + validation.get("player_count"));
        System.out.println("snapshot_count=" + validation.get("snapshot_count"));
        System.out.println("snapshot_time_range=" + validation.get("snapshot_time_range_seconds"));
        System.out.println("duplicate_keys=" + ((List<?>) validation.get("duplicate_keys")).size());
        System.out.println("final_state_mismatches="
                + ((List<?>) ((Map<?, ?>) validation.get("final_state_comparison")).get("mismatches")).size());
    }

    private Map<String, Object> run() throws Exception {
        Demo.CDemoFileInfo replayInfo = Clarity.infoForFile(replayPath.toString());
        Object matchMetadata;
        try {
            DOTAS2MatchMetadata.CDOTAMatchMetadataFile metadata = Clarity.metadataForFile(replayPath.toString());
            matchMetadata = protoToMap(metadata);
        } catch (Exception exception) {
            matchMetadata = mapOf(
                    "available", false,
                    "error", exception.getClass().getSimpleName() + ": " + exception.getMessage()
            );
        }

        try (MappedFileSource source = new MappedFileSource(replayPath.toString())) {
            new SimpleRunner(source).runWith(this);
        }

        List<Map<String, Object>> finalPlayers = entities == null ? List.of() : extractPlayers();
        Map<String, Object> finalState = mapOf(
                "source_game_time_seconds", lastGameTime,
                "direct_game_time_property_seconds", lastDirectGameTime,
                "replay_tick", lastReplayTick,
                "raw_replay_time_seconds", lastRawReplayTime,
                "game_state", lastGameState,
                "players", finalPlayers
        );

        Map<String, Object> root = new LinkedHashMap<>();
        root.put("schema_version", 1);
        root.put("replay", replayMetadata(replayInfo, matchMetadata));
        root.put("clock_normalization", clockMetadata());
        root.put("snapshot_schema", snapshotSchema());
        root.put("snapshots", snapshots);
        root.put("final_replay_entity_state", finalState);
        root.put("version_dependent_property_inventory", collectRelevantProperties());
        root.put("validation", validate(finalPlayers));
        return root;
    }

    @OnTickEnd
    public void onTickEnd(Context context, boolean synthetic) {
        int tick = context.getTick();
        double rawReplayTime = tick * context.getMillisPerTick() / 1000.0;
        lastReplayTick = tick;
        lastRawReplayTime = rawReplayTime;

        Entity rules = entities.getByDtName(GAME_RULES_ENTITY);
        Float directGameTime = null;
        for (String candidate : GAME_TIME_PATHS) {
            directGameTime = readFloatDirect(rules, candidate);
            if (directGameTime != null) {
                actualGameTimePath = candidate;
                break;
            }
        }
        Integer gameState = readIntegerDirect(rules, GAME_STATE_PATH);
        Boolean gamePaused = readBooleanDirect(rules, GAME_PAUSED_PATH);
        Integer totalPausedTicks = readIntegerDirect(rules, TOTAL_PAUSED_TICKS_PATH);
        Integer pauseStartTick = readIntegerDirect(rules, PAUSE_START_TICK_PATH);
        Float gameStartTime = readFloatDirect(rules, GAME_START_TIME_PATH);
        Float gameEndTime = readFloatDirect(rules, GAME_END_TIME_PATH);
        lastDirectGameTime = directGameTime;
        lastGameState = gameState;
        clock.observe(
                tick,
                rawReplayTime,
                context.getMillisPerTick(),
                directGameTime,
                gameState,
                gamePaused,
                totalPausedTicks,
                pauseStartTick,
                gameStartTime,
                gameEndTime
        );
        Float sourceGameTime = clock.normalizedGameTime();
        lastGameTime = sourceGameTime;

        if (!clock.isProven() || sourceGameTime == null || sourceGameTime < nextSnapshotSecond) {
            return;
        }

        List<Map<String, Object>> players = extractPlayers();
        if (players.isEmpty()) {
            return;
        }
        while (sourceGameTime >= nextSnapshotSecond) {
            snapshots.add(mapOf(
                    "game_time_seconds", nextSnapshotSecond,
                    "source_game_time_seconds", sourceGameTime,
                    "direct_game_time_property_seconds", directGameTime,
                    "replay_tick", tick,
                    "raw_replay_time_seconds", rawReplayTime,
                    "game_state", gameState,
                    "players", deepCopyRows(players)
            ));
            nextSnapshotSecond += SNAPSHOT_INTERVAL_SECONDS;
        }
    }

    private List<Map<String, Object>> extractPlayers() {
        Entity playerResource = entities.getByDtName("CDOTA_PlayerResource");
        if (playerResource == null) {
            return List.of();
        }

        List<PlayerIndex> realPlayers = new ArrayList<>();
        Map<Integer, Integer> positions = new HashMap<>();
        for (int index = 0; index < 64; index++) {
            String arrayIndex = Util.arrayIdxToString(index);
            Integer team = readInteger("team", playerResource, List.of(
                    "m_vecPlayerData." + arrayIndex + ".m_iPlayerTeam"
            ));
            if (team == null || (team != 2 && team != 3)) {
                continue;
            }
            int teamPosition = positions.getOrDefault(team, 0);
            positions.put(team, teamPosition + 1);
            realPlayers.add(new PlayerIndex(index, team, teamPosition));
        }

        List<Map<String, Object>> rows = new ArrayList<>();
        for (PlayerIndex player : realPlayers) {
            rows.add(extractPlayer(playerResource, player));
        }
        return rows;
    }

    private Map<String, Object> extractPlayer(Entity playerResource, PlayerIndex player) {
        String playerIndex = Util.arrayIdxToString(player.index());
        String teamPosition = Util.arrayIdxToString(player.teamPosition());
        String dataEntityName = player.team() == 2 ? "CDOTA_DataRadiant" : "CDOTA_DataDire";
        Entity dataEntity = entities.getByDtName(dataEntityName);

        recordStructuralSource("player_slot", "CDOTA_PlayerResource.m_vecPlayerData array index");
        String selectedHeroPath = "m_vecPlayerTeamData." + playerIndex + ".m_hSelectedHero";
        Integer heroHandle = readIntegerInternal(playerResource, List.of(selectedHeroPath), "hero_name");
        if (heroHandle != null) {
            fieldProvenance.get("items").actualPaths.add(
                    playerResource.getDtClass().getDtName() + "." + selectedHeroPath + " -> selected hero entity"
            );
        }
        Entity hero = heroHandle == null ? null : entities.getByHandle(heroHandle);

        Integer heroId = readInteger("hero_id", playerResource, List.of(
                "m_vecPlayerTeamData." + playerIndex + ".m_nSelectedHeroID",
                "m_vecPlayerData." + playerIndex + ".m_nSelectedHeroID",
                "m_vecPlayerTeamData." + playerIndex + ".m_iSelectedHeroID"
        ));
        String heroName = resolveEntityName("hero_name", hero);

        Integer level = firstInteger("level",
                new EntityCandidates(playerResource, List.of(
                        "m_vecPlayerTeamData." + playerIndex + ".m_iLevel"
                )),
                new EntityCandidates(hero, List.of("m_iCurrentLevel"))
        );
        Integer kills = readInteger("kills", playerResource, List.of(
                "m_vecPlayerTeamData." + playerIndex + ".m_iKills"
        ));
        Integer deaths = readInteger("deaths", playerResource, List.of(
                "m_vecPlayerTeamData." + playerIndex + ".m_iDeaths"
        ));
        Integer assists = readInteger("assists", playerResource, List.of(
                "m_vecPlayerTeamData." + playerIndex + ".m_iAssists"
        ));
        Integer lastHits = readInteger("last_hits", dataEntity, List.of(
                "m_vecDataTeam." + teamPosition + ".m_iLastHitCount"
        ));
        Integer denies = readInteger("denies", dataEntity, List.of(
                "m_vecDataTeam." + teamPosition + ".m_iDenyCount"
        ));
        Integer netWorth = firstInteger("net_worth",
                new EntityCandidates(dataEntity, List.of(
                        "m_vecDataTeam." + teamPosition + ".m_iNetWorth"
                )),
                new EntityCandidates(playerResource, List.of(
                        "m_vecPlayerTeamData." + playerIndex + ".m_iNetWorth"
                )),
                new EntityCandidates(hero, List.of("m_iNetWorth"))
        );
        Integer currentGold = currentGold(playerResource, dataEntity, playerIndex, teamPosition);
        Integer totalXp = firstInteger("total_xp",
                new EntityCandidates(dataEntity, List.of(
                        "m_vecDataTeam." + teamPosition + ".m_iTotalEarnedXP",
                        "m_vecDataTeam." + teamPosition + ".m_iTotalXP"
                )),
                new EntityCandidates(playerResource, List.of(
                        "m_vecPlayerTeamData." + playerIndex + ".m_iTotalEarnedXP",
                        "m_vecPlayerTeamData." + playerIndex + ".m_iTotalXP"
                )),
                new EntityCandidates(hero, List.of("m_iCurrentXP"))
        );
        List<Map<String, Object>> items = extractItems(hero);

        return mapOf(
                "player_slot", player.index(),
                "team", player.team() == 2 ? "RADIANT" : "DIRE",
                "hero_id", heroId,
                "hero_name", heroName,
                "level", level,
                "kills", kills,
                "deaths", deaths,
                "assists", assists,
                "last_hits", lastHits,
                "denies", denies,
                "net_worth", netWorth,
                "current_gold", currentGold,
                "total_xp", totalXp,
                "items", items
        );
    }

    private Integer currentGold(Entity playerResource, Entity dataEntity, String playerIndex, String teamPosition) {
        Integer direct = firstInteger("current_gold",
                new EntityCandidates(dataEntity, List.of(
                        "m_vecDataTeam." + teamPosition + ".m_iGold"
                )),
                new EntityCandidates(playerResource, List.of(
                        "m_vecPlayerTeamData." + playerIndex + ".m_iGold"
                ))
        );
        if (direct != null) {
            return direct;
        }

        Integer reliable = firstInteger("current_gold",
                new EntityCandidates(dataEntity, List.of(
                        "m_vecDataTeam." + teamPosition + ".m_iReliableGold"
                )),
                new EntityCandidates(playerResource, List.of(
                        "m_vecPlayerTeamData." + playerIndex + ".m_iReliableGold"
                ))
        );
        Integer unreliable = firstInteger("current_gold",
                new EntityCandidates(dataEntity, List.of(
                        "m_vecDataTeam." + teamPosition + ".m_iUnreliableGold"
                )),
                new EntityCandidates(playerResource, List.of(
                        "m_vecPlayerTeamData." + playerIndex + ".m_iUnreliableGold"
                ))
        );
        if (reliable == null && unreliable == null) {
            return null;
        }
        fieldProvenance.get("current_gold").notes.add(
                "computed only as the sum of direct current-state reliable and unreliable gold properties"
        );
        return (reliable == null ? 0 : reliable) + (unreliable == null ? 0 : unreliable);
    }

    private List<Map<String, Object>> extractItems(Entity hero) {
        PropertyProvenance provenance = fieldProvenance.get("items");
        if (hero == null) {
            provenance.notes.add("selected hero entity was unavailable at one or more snapshots");
            return null;
        }

        List<FieldPathName> inventoryPaths = new ArrayList<>();
        for (int slot = 0; slot < 24; slot++) {
            String path = "m_hItems." + Util.arrayIdxToString(slot);
            provenance.attemptedPaths.add(hero.getDtClass().getDtName() + "." + path);
            FieldPath fieldPath = hero.getDtClass().getFieldPathForName(path);
            if (fieldPath != null) {
                provenance.actualPaths.add(hero.getDtClass().getDtName() + "." + path);
                inventoryPaths.add(new FieldPathName(slot, path, fieldPath));
            }
        }
        if (inventoryPaths.isEmpty()) {
            provenance.notes.add("no m_hItems array property was present on the selected hero entity class");
            return null;
        }

        List<Map<String, Object>> result = new ArrayList<>();
        for (FieldPathName inventoryPath : inventoryPaths) {
            Object rawHandle = safeProperty(hero, inventoryPath.fieldPath());
            if (!(rawHandle instanceof Number number)) {
                continue;
            }
            int handle = number.intValue();
            Entity item = entities.getByHandle(handle);
            if (item == null) {
                continue;
            }
            String itemName = resolveItemName(item, provenance);
            result.add(mapOf(
                    "inventory_slot", inventoryPath.slot(),
                    "item_name", itemName,
                    "entity_class", item.getDtClass().getDtName(),
                    "entity_handle", handle,
                    "source_property_path", hero.getDtClass().getDtName() + "." + inventoryPath.name()
            ));
        }
        return result;
    }

    private String resolveItemName(Entity item, PropertyProvenance provenance) {
        provenance.actualPaths.add(item.getDtClass().getDtName() + ".DTClass.getDtName()");
        provenance.notes.add("item_name is the exact Clarity item entity DT class, not a localized display name");
        return item.getDtClass().getDtName();
    }

    private String resolveEntityName(String field, Entity entity) {
        PropertyProvenance provenance = fieldProvenance.get(field);
        if (entity == null) {
            provenance.notes.add("selected hero handle did not resolve at one or more snapshots");
            return null;
        }
        provenance.actualPaths.add(entity.getDtClass().getDtName() + ".DTClass.getDtName()");
        provenance.notes.add("hero_name is the exact Clarity hero entity DT class, not a localized display name");
        return entity.getDtClass().getDtName();
    }

    private Integer firstInteger(String field, EntityCandidates... groups) {
        for (EntityCandidates group : groups) {
            Integer value = readInteger(field, group.entity(), group.paths());
            if (value != null) {
                return value;
            }
        }
        return null;
    }

    private Integer readInteger(String field, Entity entity, List<String> paths) {
        return readIntegerInternal(entity, paths, field);
    }

    private Integer readIntegerInternal(Entity entity, List<String> paths, String field) {
        PropertyProvenance provenance = field == null ? null : fieldProvenance.get(field);
        String entityName = entity == null ? "<entity-unavailable>" : entity.getDtClass().getDtName();
        for (String path : paths) {
            if (provenance != null) {
                provenance.attemptedPaths.add(entityName + "." + path);
            }
            if (entity == null) {
                continue;
            }
            FieldPath fieldPath = entity.getDtClass().getFieldPathForName(path);
            if (fieldPath == null) {
                continue;
            }
            if (provenance != null) {
                provenance.actualPaths.add(entityName + "." + path);
            }
            Object value = safeProperty(entity, fieldPath);
            if (value instanceof Number number) {
                return number.intValue();
            }
        }
        return null;
    }

    private Float readFloatDirect(Entity entity, String path) {
        if (entity == null) {
            return null;
        }
        FieldPath fieldPath = entity.getDtClass().getFieldPathForName(path);
        if (fieldPath == null) {
            return null;
        }
        Object value = safeProperty(entity, fieldPath);
        return value instanceof Number number ? number.floatValue() : null;
    }

    private Integer readIntegerDirect(Entity entity, String path) {
        if (entity == null) {
            return null;
        }
        FieldPath fieldPath = entity.getDtClass().getFieldPathForName(path);
        if (fieldPath == null) {
            return null;
        }
        Object value = safeProperty(entity, fieldPath);
        return value instanceof Number number ? number.intValue() : null;
    }

    private Boolean readBooleanDirect(Entity entity, String path) {
        if (entity == null) {
            return null;
        }
        FieldPath fieldPath = entity.getDtClass().getFieldPathForName(path);
        if (fieldPath == null) {
            return null;
        }
        Object value = safeProperty(entity, fieldPath);
        return value instanceof Boolean bool ? bool : null;
    }

    private Object safeProperty(Entity entity, FieldPath path) {
        try {
            return entity.getPropertyForFieldPath(path);
        } catch (RuntimeException ignored) {
            return null;
        }
    }

    private void recordStructuralSource(String field, String source) {
        fieldProvenance.get(field).actualPaths.add(source);
    }

    private Map<String, Object> replayMetadata(Demo.CDemoFileInfo replayInfo, Object matchMetadata)
            throws IOException, NoSuchAlgorithmException {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("source_path", replayPath.toString());
        result.put("file_size_bytes", Files.size(replayPath));
        result.put("sha256", sha256(replayPath));
        result.put("clarity_version", "4.0.1");
        result.put("demo_file_info", protoToMap(replayInfo));
        result.put("dota_match_metadata", matchMetadata);
        return result;
    }

    private Map<String, Object> clockMetadata() {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("status", clock.isProven() ? "PROVEN" : "UNRESOLVED");
        result.put("normalization_method", clock.mode() == null
                ? "unresolved; no fixed offset used"
                : clock.mode());
        result.put("signal", clock.mode() == ClockMode.DIRECT_GAME_TIME
                ? mapOf(
                        "type", "entity_property",
                        "entity_dt_name", GAME_RULES_ENTITY,
                        "property_path", actualGameTimePath,
                        "attempted_property_paths", GAME_TIME_PATHS,
                        "units", "seconds",
                        "semantics", "Clarity current entity state for the Dota game clock"
                )
                : mapOf(
                        "type", "entity_property_transition",
                        "entity_dt_name", GAME_RULES_ENTITY,
                        "property_path", GAME_START_TIME_PATH,
                        "transition", "nonpositive to positive engine game-start timestamp",
                        "corroborating_property_path", GAME_STATE_PATH,
                        "normalized_time_formula",
                        "(replay_tick - zero_tick - completed_paused_ticks - active_pause_ticks)"
                                + " * Context.getMillisPerTick() / 1000",
                        "pause_properties", List.of(TOTAL_PAUSED_TICKS_PATH, GAME_PAUSED_PATH, PAUSE_START_TICK_PATH),
                        "fixed_offset_used", false
                ));
        if (clock.isProven()) {
            result.put("zero_boundary", mapOf(
                    "criterion", clock.mode() == ClockMode.DIRECT_GAME_TIME
                            ? "witnessed direct game-clock property transition from negative to nonnegative"
                            : "witnessed m_flGameStartTime transition from nonpositive to positive",
                    "previous_replay_tick", clock.previousTick(),
                    "previous_raw_replay_time_seconds", clock.previousRawReplayTime(),
                    "previous_source_game_time_seconds", clock.previousGameTime(),
                    "replay_tick", clock.zeroTick(),
                    "raw_replay_time_seconds", clock.zeroRawReplayTime(),
                    "source_game_time_seconds", clock.zeroGameTime(),
                    "previous_game_start_time_property_seconds", clock.previousGameStartTime(),
                    "game_start_time_property_seconds", clock.zeroGameStartTime(),
                    "previous_game_state", clock.previousGameState(),
                    "game_state", clock.zeroGameState()
            ));
        } else {
            result.put("zero_boundary", null);
            result.put("unresolved_reason",
                    "the parser witnessed neither a direct game-clock zero crossing nor m_flGameStartTime become positive");
        }
        result.put("game_state_transitions", clock.gameStateTransitions());
        result.put("engine_time_property_transitions", clock.engineTimeTransitions());
        result.put("game_end_boundary", mapOf(
                "property_path", GAME_END_TIME_PATH,
                "replay_tick", clock.gameEndTick(),
                "raw_replay_time_seconds", clock.gameEndRawReplayTime(),
                "property_value_seconds", clock.gameEndPropertyTime()
        ));
        result.put("pause_accounting", mapOf(
                "total_paused_ticks_property", GAME_RULES_ENTITY + "." + TOTAL_PAUSED_TICKS_PATH,
                "active_pause_property", GAME_RULES_ENTITY + "." + GAME_PAUSED_PATH,
                "pause_start_tick_property", GAME_RULES_ENTITY + "." + PAUSE_START_TICK_PATH,
                "baseline_total_paused_ticks", clock.baselineTotalPausedTicks(),
                "last_total_paused_ticks", clock.lastTotalPausedTicks(),
                "witnessed_active_pause", clock.witnessedActivePause()
        ));
        result.put("raw_time_distinction", mapOf(
                "replay_tick", "raw demo tick reported by Clarity Context.getTick()",
                "raw_replay_time_seconds", "replay_tick * Context.getMillisPerTick() / 1000; replay timeline only",
                "source_game_time_seconds", "direct normalized game clock property; never derived from raw replay time",
                "start_property_derived_game_time_seconds",
                "when the direct property is absent, exact tick integration from the witnessed game-start property transition"
                        + " with entity pause counters",
                "combat_log_timestamp", "not used"
        ));
        return result;
    }

    private Map<String, Object> snapshotSchema() {
        Map<String, Object> fields = new LinkedHashMap<>();
        fields.put("game_time_seconds", mapOf(
                "type", "integer",
                "meaning", "scheduled normalized snapshot time: 0, 60, 120, ...",
                "source", GAME_RULES_ENTITY + "." + (actualGameTimePath == null ? GAME_TIME_PATHS : actualGameTimePath)
                        + " threshold crossing"
        ));
        fields.put("source_game_time_seconds", mapOf(
                "type", "number",
                "meaning", "normalized source clock value at capture",
                "source", clock.mode() == ClockMode.DIRECT_GAME_TIME
                        ? GAME_RULES_ENTITY + "." + actualGameTimePath
                        : GAME_RULES_ENTITY + "." + GAME_START_TIME_PATH + " transition plus pause-adjusted replay ticks"
        ));
        fields.put("direct_game_time_property_seconds", mapOf(
                "type", "number or null",
                "meaning", "direct game-clock property value, retained separately when present",
                "source", actualGameTimePath == null ? "unavailable; attempted " + GAME_TIME_PATHS : actualGameTimePath
        ));
        fields.put("replay_tick", mapOf(
                "type", "integer",
                "source", "Clarity Context.getTick()"
        ));
        fields.put("raw_replay_time_seconds", mapOf(
                "type", "number",
                "source", "replay_tick * Clarity Context.getMillisPerTick() / 1000"
        ));

        Map<String, Object> playerFields = new LinkedHashMap<>();
        for (String field : REQUESTED_PLAYER_FIELDS) {
            PropertyProvenance provenance = fieldProvenance.get(field);
            playerFields.put(field, mapOf(
                    "nullable", !field.equals("player_slot") && !field.equals("team"),
                    "actual_property_paths", new ArrayList<>(provenance.actualPaths),
                    "attempted_property_paths", new ArrayList<>(provenance.attemptedPaths),
                    "notes", new ArrayList<>(provenance.notes)
            ));
        }
        fields.put("players", playerFields);
        return mapOf(
                "snapshot_interval_seconds", SNAPSHOT_INTERVAL_SECONDS,
                "one_row_per", "CDOTA_PlayerResource array entry whose direct m_iPlayerTeam value is 2 or 3",
                "fields", fields
        );
    }

    private Map<String, Object> validate(List<Map<String, Object>> finalPlayers) {
        int rowCount = snapshots.stream()
                .mapToInt(snapshot -> players(snapshot).size())
                .sum();
        Map<String, Object> missingness = new LinkedHashMap<>();
        for (String field : List.of(
                "game_time_seconds", "replay_tick", "player_slot", "team", "hero_id", "hero_name", "level",
                "kills", "deaths", "assists", "last_hits", "denies", "net_worth", "current_gold", "total_xp",
                "items")) {
            int missing = 0;
            for (Map<String, Object> snapshot : snapshots) {
                for (Map<String, Object> player : players(snapshot)) {
                    Object value = field.equals("game_time_seconds") || field.equals("replay_tick")
                            ? snapshot.get(field)
                            : player.get(field);
                    if (value == null) {
                        missing++;
                    }
                }
            }
            missingness.put(field, mapOf(
                    "missing", missing,
                    "total", rowCount,
                    "missing_fraction", rowCount == 0 ? null : (double) missing / rowCount
            ));
        }

        List<Integer> playerCounts = snapshots.stream().map(snapshot -> players(snapshot).size()).toList();
        boolean scheduledMonotonic = strictlyIncreasing(snapshotNumbers("game_time_seconds"));
        boolean sourceMonotonic = nondecreasing(snapshotNumbers("source_game_time_seconds"));
        List<Map<String, Object>> duplicateKeys = findDuplicateKeys(snapshots);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("player_count", finalPlayers.size());
        result.put("players_per_snapshot_min", playerCounts.stream().min(Integer::compareTo).orElse(0));
        result.put("players_per_snapshot_max", playerCounts.stream().max(Integer::compareTo).orElse(0));
        result.put("snapshot_count", snapshots.size());
        result.put("snapshot_time_range_seconds", snapshots.isEmpty()
                ? null
                : List.of(snapshots.get(0).get("game_time_seconds"),
                        snapshots.get(snapshots.size() - 1).get("game_time_seconds")));
        result.put("scheduled_game_time_strictly_monotonic", scheduledMonotonic);
        result.put("source_game_time_monotonic", sourceMonotonic);
        result.put("duplicate_keys", duplicateKeys);
        result.put("missingness", missingness);
        result.put("final_state_comparison", compareFinalState(finalPlayers));
        result.put("normalization_guard", mapOf(
                "passed", clock.isProven() ? !snapshots.isEmpty() : snapshots.isEmpty(),
                "rule", "unresolved normalization must produce zero snapshots"
        ));
        return result;
    }

    private Map<String, Object> compareFinalState(List<Map<String, Object>> finalPlayers) {
        if (snapshots.isEmpty()) {
            return mapOf(
                    "comparable", false,
                    "reason", "no normalized snapshots",
                    "mismatches", List.of(),
                    "unavailable_comparisons", List.of()
            );
        }
        Map<String, Object> lastSnapshot = snapshots.get(snapshots.size() - 1);
        Map<Integer, Map<String, Object>> finalBySlot = indexBySlot(finalPlayers);
        List<Map<String, Object>> mismatches = new ArrayList<>();
        List<Map<String, Object>> unavailable = new ArrayList<>();
        for (Map<String, Object> snapshotPlayer : players(lastSnapshot)) {
            int slot = ((Number) snapshotPlayer.get("player_slot")).intValue();
            Map<String, Object> finalPlayer = finalBySlot.get(slot);
            if (finalPlayer == null) {
                unavailable.add(mapOf("player_slot", slot, "reason", "player absent from replay-end entity state"));
                continue;
            }
            for (String field : FINAL_COMPARISON_FIELDS) {
                Object snapshotValue = snapshotPlayer.get(field);
                Object finalValue = finalPlayer.get(field);
                if (snapshotValue == null || finalValue == null) {
                    unavailable.add(mapOf(
                            "player_slot", slot,
                            "field", field,
                            "snapshot_value", snapshotValue,
                            "replay_end_value", finalValue
                    ));
                } else if (!numbersEqual(snapshotValue, finalValue)) {
                    mismatches.add(mapOf(
                            "player_slot", slot,
                            "field", field,
                            "snapshot_value", snapshotValue,
                            "replay_end_value", finalValue
                    ));
                }
            }
        }
        return mapOf(
                "comparable", true,
                "basis", "last scheduled snapshot versus independently re-read Clarity entity state at replay end",
                "last_snapshot_game_time_seconds", lastSnapshot.get("game_time_seconds"),
                "last_snapshot_source_game_time_seconds", lastSnapshot.get("source_game_time_seconds"),
                "replay_end_source_game_time_seconds", lastGameTime,
                "note", "mismatches can be legitimate changes during the final partial minute",
                "mismatches", mismatches,
                "unavailable_comparisons", unavailable
        );
    }

    private Map<String, Object> collectRelevantProperties() {
        Map<String, Set<String>> byEntity = new LinkedHashMap<>();
        if (entities == null) {
            return Map.of();
        }
        Iterator<Entity> iterator = entities.getAllByPredicate(entity -> {
            String name = entity.getDtClass().getDtName();
            return name.equals("CDOTA_PlayerResource")
                    || name.equals("CDOTA_DataRadiant")
                    || name.equals("CDOTA_DataDire")
                    || name.equals(GAME_RULES_ENTITY)
                    || name.startsWith("CDOTA_Unit_Hero_");
        });
        while (iterator.hasNext()) {
            Entity entity = iterator.next();
            Set<String> paths = byEntity.computeIfAbsent(entity.getDtClass().getDtName(), ignored -> new TreeSet<>());
            for (FieldPath path : entity.getDtClass().collectFieldPaths(entity.getState())) {
                String name = entity.getDtClass().getNameForFieldPath(path);
                String lower = name.toLowerCase(Locale.ROOT);
                boolean gameRulesTiming = entity.getDtClass().getDtName().equals(GAME_RULES_ENTITY)
                        && (lower.contains("time") || lower.contains("state") || lower.contains("pause"));
                if (gameRulesTiming || lower.contains("gametime") || lower.contains("gamestate") || lower.contains("playerteam")
                        || lower.contains("selectedhero") || lower.contains("level") || lower.contains("kill")
                        || lower.contains("death") || lower.contains("assist") || lower.contains("gold")
                        || lower.contains("lasthit") || lower.contains("deny") || lower.contains("worth")
                        || lower.contains("xp") || lower.contains("item") || lower.contains("unitname")) {
                    paths.add(name);
                }
            }
        }
        Map<String, Object> result = new LinkedHashMap<>();
        byEntity.forEach((entity, paths) -> result.put(entity, new ArrayList<>(paths)));
        return result;
    }

    static List<Map<String, Object>> findDuplicateKeys(List<Map<String, Object>> snapshots) {
        Set<String> seen = new LinkedHashSet<>();
        List<Map<String, Object>> duplicates = new ArrayList<>();
        for (Map<String, Object> snapshot : snapshots) {
            Object gameTime = snapshot.get("game_time_seconds");
            for (Map<String, Object> player : players(snapshot)) {
                Object slot = player.get("player_slot");
                String key = gameTime + ":" + slot;
                if (!seen.add(key)) {
                    duplicates.add(mapOf("game_time_seconds", gameTime, "player_slot", slot));
                }
            }
        }
        return duplicates;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> players(Map<String, Object> snapshot) {
        return (List<Map<String, Object>>) snapshot.get("players");
    }

    private List<Number> snapshotNumbers(String field) {
        List<Number> values = new ArrayList<>();
        for (Map<String, Object> snapshot : snapshots) {
            Object value = snapshot.get(field);
            if (value instanceof Number number) {
                values.add(number);
            }
        }
        return values;
    }

    private static boolean strictlyIncreasing(List<Number> values) {
        for (int i = 1; i < values.size(); i++) {
            if (values.get(i).doubleValue() <= values.get(i - 1).doubleValue()) {
                return false;
            }
        }
        return true;
    }

    private static boolean nondecreasing(List<Number> values) {
        for (int i = 1; i < values.size(); i++) {
            if (values.get(i).doubleValue() < values.get(i - 1).doubleValue()) {
                return false;
            }
        }
        return true;
    }

    private static boolean numbersEqual(Object left, Object right) {
        if (left instanceof Number l && right instanceof Number r) {
            return Double.compare(l.doubleValue(), r.doubleValue()) == 0;
        }
        return Objects.equals(left, right);
    }

    private static Map<Integer, Map<String, Object>> indexBySlot(List<Map<String, Object>> players) {
        Map<Integer, Map<String, Object>> result = new HashMap<>();
        for (Map<String, Object> player : players) {
            Object slot = player.get("player_slot");
            if (slot instanceof Number number) {
                result.put(number.intValue(), player);
            }
        }
        return result;
    }

    private static List<Map<String, Object>> deepCopyRows(List<Map<String, Object>> rows) {
        List<Map<String, Object>> copy = new ArrayList<>();
        for (Map<String, Object> row : rows) {
            Map<String, Object> rowCopy = new LinkedHashMap<>(row);
            Object items = row.get("items");
            if (items instanceof List<?> list) {
                rowCopy.put("items", new ArrayList<>(list));
            }
            copy.add(rowCopy);
        }
        return copy;
    }

    private static Map<String, Object> protoToMap(Message message) {
        Map<String, Object> result = new LinkedHashMap<>();
        message.getAllFields().entrySet().stream()
                .sorted(Comparator.comparingInt(entry -> entry.getKey().getNumber()))
                .forEach(entry -> result.put(entry.getKey().getName(), protoValue(entry.getValue())));
        return result;
    }

    private static Object protoValue(Object value) {
        if (value instanceof Message message) {
            return protoToMap(message);
        }
        if (value instanceof ByteString bytes) {
            try {
                MessageDigest digest = MessageDigest.getInstance("SHA-256");
                return mapOf(
                        "size_bytes", bytes.size(),
                        "sha256", hex(digest.digest(bytes.toByteArray())),
                        "encoding", "opaque protobuf bytes; content not expanded"
                );
            } catch (NoSuchAlgorithmException impossible) {
                return Base64.getEncoder().encodeToString(bytes.toByteArray());
            }
        }
        if (value instanceof Descriptors.EnumValueDescriptor enumValue) {
            return mapOf("name", enumValue.getName(), "number", enumValue.getNumber());
        }
        if (value instanceof List<?> list) {
            return list.stream().map(ReplayProbe::protoValue).toList();
        }
        return value;
    }

    private static String sha256(Path path) throws IOException, NoSuchAlgorithmException {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (var input = Files.newInputStream(path)) {
            byte[] buffer = new byte[1024 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) {
                digest.update(buffer, 0, count);
            }
        }
        return hex(digest.digest());
    }

    private static String hex(byte[] bytes) {
        StringBuilder result = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            result.append(String.format("%02x", value));
        }
        return result.toString();
    }

    @SafeVarargs
    static <K, V> Map<K, V> mapOf(Object... pairs) {
        if (pairs.length % 2 != 0) {
            throw new IllegalArgumentException("mapOf requires key/value pairs");
        }
        Map<K, V> result = new LinkedHashMap<>();
        for (int i = 0; i < pairs.length; i += 2) {
            @SuppressWarnings("unchecked") K key = (K) pairs[i];
            @SuppressWarnings("unchecked") V value = (V) pairs[i + 1];
            result.put(key, value);
        }
        return result;
    }

    enum ClockMode {
        DIRECT_GAME_TIME,
        GAME_START_TIME_AND_PAUSE_TICKS
    }

    static final class ClockTracker {
        private Integer lastTick;
        private Double lastRawReplayTime;
        private Float lastGameTime;
        private Integer lastGameState;
        private Boolean lastGamePaused;
        private Integer lastTotalPausedTicks;
        private Float lastGameStartTime;
        private Float lastGameEndTime;
        private Integer baselineTotalPausedTicks;
        private Integer previousTick;
        private Double previousRawReplayTime;
        private Float previousGameTime;
        private Integer previousGameState;
        private Integer zeroTick;
        private Double zeroRawReplayTime;
        private Float zeroGameTime;
        private Integer zeroGameState;
        private Float previousGameStartTime;
        private Float zeroGameStartTime;
        private Integer gameEndTick;
        private Double gameEndRawReplayTime;
        private Float gameEndPropertyTime;
        private Float normalizedGameTime;
        private ClockMode mode;
        private boolean witnessedActivePause;
        private final List<Map<String, Object>> gameStateTransitions = new ArrayList<>();
        private final List<Map<String, Object>> engineTimeTransitions = new ArrayList<>();

        void observe(int tick, double rawReplayTime, Float gameTime, Integer gameState) {
            observe(tick, rawReplayTime, 1000.0f / 30.0f, gameTime, gameState, false, 0, null, null, null);
        }

        void observe(
                int tick,
                double rawReplayTime,
                float millisPerTick,
                Float directGameTime,
                Integer gameState,
                Boolean gamePaused,
                Integer totalPausedTicks,
                Integer pauseStartTick,
                Float gameStartTime,
                Float gameEndTime
        ) {
            if (gameState != null && !Objects.equals(gameState, lastGameState)) {
                gameStateTransitions.add(mapOf(
                        "replay_tick", tick,
                        "raw_replay_time_seconds", rawReplayTime,
                        "from", lastGameState,
                        "to", gameState
                ));
            }
            if (Boolean.TRUE.equals(gamePaused)) {
                witnessedActivePause = true;
            }
            if (gameStartTime != null && !Objects.equals(gameStartTime, lastGameStartTime)) {
                engineTimeTransitions.add(mapOf(
                        "property_path", GAME_START_TIME_PATH,
                        "replay_tick", tick,
                        "raw_replay_time_seconds", rawReplayTime,
                        "from", lastGameStartTime,
                        "to", gameStartTime,
                        "game_state", gameState
                ));
            }
            if (gameEndTime != null && !Objects.equals(gameEndTime, lastGameEndTime)) {
                engineTimeTransitions.add(mapOf(
                        "property_path", GAME_END_TIME_PATH,
                        "replay_tick", tick,
                        "raw_replay_time_seconds", rawReplayTime,
                        "from", lastGameEndTime,
                        "to", gameEndTime,
                        "game_state", gameState
                ));
            }

            if (mode == null && directGameTime != null && lastGameTime != null
                    && lastGameTime < 0.0f && directGameTime >= 0.0f) {
                mode = ClockMode.DIRECT_GAME_TIME;
                previousTick = lastTick;
                previousRawReplayTime = lastRawReplayTime;
                previousGameTime = lastGameTime;
                previousGameState = lastGameState;
                zeroTick = tick;
                zeroRawReplayTime = rawReplayTime;
                zeroGameTime = directGameTime;
                zeroGameState = gameState;
            }
            if (mode == null && lastGameStartTime != null && lastGameStartTime <= 0.0f
                    && gameStartTime != null && gameStartTime > 0.0f) {
                mode = ClockMode.GAME_START_TIME_AND_PAUSE_TICKS;
                previousTick = lastTick;
                previousRawReplayTime = lastRawReplayTime;
                previousGameTime = null;
                previousGameState = lastGameState;
                previousGameStartTime = lastGameStartTime;
                zeroTick = tick;
                zeroRawReplayTime = rawReplayTime;
                zeroGameTime = 0.0f;
                zeroGameState = gameState;
                zeroGameStartTime = gameStartTime;
                baselineTotalPausedTicks = totalPausedTicks == null ? 0 : totalPausedTicks;
                normalizedGameTime = 0.0f;
            }

            if (mode == ClockMode.DIRECT_GAME_TIME) {
                normalizedGameTime = directGameTime;
            } else if (mode == ClockMode.GAME_START_TIME_AND_PAUSE_TICKS && zeroTick != null
                    && gameEndTick == null) {
                int completedPausedTicks = Math.max(0,
                        (totalPausedTicks == null ? baselineTotalPausedTicks : totalPausedTicks)
                                - (baselineTotalPausedTicks == null ? 0 : baselineTotalPausedTicks));
                int activePauseTicks = Boolean.TRUE.equals(gamePaused) && pauseStartTick != null
                        ? Math.max(0, tick - pauseStartTick)
                        : 0;
                int activeTicks = Math.max(0, tick - zeroTick - completedPausedTicks - activePauseTicks);
                normalizedGameTime = activeTicks * millisPerTick / 1000.0f;
            }
            if (mode == ClockMode.GAME_START_TIME_AND_PAUSE_TICKS && gameEndTick == null
                    && lastGameEndTime != null && lastGameEndTime <= 0.0f
                    && gameEndTime != null && gameEndTime > 0.0f) {
                gameEndTick = tick;
                gameEndRawReplayTime = rawReplayTime;
                gameEndPropertyTime = gameEndTime;
            }
            lastTick = tick;
            lastRawReplayTime = rawReplayTime;
            lastGameTime = directGameTime;
            lastGameState = gameState;
            lastGamePaused = gamePaused;
            lastTotalPausedTicks = totalPausedTicks;
            lastGameStartTime = gameStartTime;
            lastGameEndTime = gameEndTime;
        }

        boolean isProven() { return zeroTick != null; }
        ClockMode mode() { return mode; }
        Float normalizedGameTime() { return normalizedGameTime; }
        Integer previousTick() { return previousTick; }
        Double previousRawReplayTime() { return previousRawReplayTime; }
        Float previousGameTime() { return previousGameTime; }
        Integer previousGameState() { return previousGameState; }
        int zeroTick() { return zeroTick == null ? -1 : zeroTick; }
        Double zeroRawReplayTime() { return zeroRawReplayTime; }
        Float zeroGameTime() { return zeroGameTime; }
        Integer zeroGameState() { return zeroGameState; }
        Float previousGameStartTime() { return previousGameStartTime; }
        Float zeroGameStartTime() { return zeroGameStartTime; }
        Integer gameEndTick() { return gameEndTick; }
        Double gameEndRawReplayTime() { return gameEndRawReplayTime; }
        Float gameEndPropertyTime() { return gameEndPropertyTime; }
        Integer baselineTotalPausedTicks() { return baselineTotalPausedTicks; }
        Integer lastTotalPausedTicks() { return lastTotalPausedTicks; }
        boolean witnessedActivePause() { return witnessedActivePause; }
        List<Map<String, Object>> gameStateTransitions() { return new ArrayList<>(gameStateTransitions); }
        List<Map<String, Object>> engineTimeTransitions() { return new ArrayList<>(engineTimeTransitions); }
    }

    private static final class PropertyProvenance {
        private final Set<String> actualPaths = new LinkedHashSet<>();
        private final Set<String> attemptedPaths = new LinkedHashSet<>();
        private final Set<String> notes = new LinkedHashSet<>();
    }

    private record PlayerIndex(int index, int team, int teamPosition) {}
    private record EntityCandidates(Entity entity, List<String> paths) {}
    private record FieldPathName(int slot, String name, FieldPath fieldPath) {}
}
