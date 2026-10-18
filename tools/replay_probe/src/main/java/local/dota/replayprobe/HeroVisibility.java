package local.dota.replayprobe;

import skadistats.clarity.io.Util;
import skadistats.clarity.model.Entity;

import java.util.Map;

/** Read current team bitsets at the same completed tick as global hero positions. */
final class HeroVisibility {
    static final String FIELD = "m_bNPCVisibleState";
    static final String RADIANT_SOURCE = "CDOTA_DataRadiant";
    static final String DIRE_SOURCE = "CDOTA_DataDire";
    private static final String UNKNOWN = "UNKNOWN";

    record Hero(HeroPositions.Source identity, int index, int serial) {}
    private record Reading(String state, Map<String, Object> evidence) {}

    private HeroVisibility() {}

    static Map<String, Object> observe(int slot, int team, Integer heroId, Integer selectedHandle,
                                       Entity hero, Entity radiant, Entity dire) {
        Hero identity = hero == null ? null : new Hero(HeroPositions.sourceOf(hero), hero.getIndex(), hero.getSerial());
        return observeSource(slot, team, heroId, selectedHandle, identity,
                HeroPositions.sourceOf(radiant), HeroPositions.sourceOf(dire));
    }

    static Map<String, Object> observeSource(int slot, int team, Integer heroId, Integer selectedHandle,
                                            Hero hero, HeroPositions.Source radiant, HeroPositions.Source dire) {
        HeroPositions.Source identity = hero == null ? null : hero.identity();
        Map<String, Object> row = ReplayProbe.mapOf(
                "player_slot", slot,
                "team", team == 2 ? "RADIANT" : "DIRE",
                "hero_id", heroId,
                "selected_hero_handle", selectedHandle,
                "hero_entity_handle", identity == null ? null : identity.handle(),
                "hero_entity_class", identity == null ? null : identity.entityClass(),
                "hero_entity_index", hero == null ? null : hero.index(),
                "hero_entity_serial", hero == null ? null : hero.serial(),
                "life_state", null,
                "visible_to_radiant", UNKNOWN,
                "visible_to_dire", UNKNOWN,
                "visibility_status", UNKNOWN
        );
        String unavailable = hero == null ? "HERO_ENTITY_UNAVAILABLE"
                : !HeroPositions.primaryIdentityMatches(team, heroId, selectedHandle, identity)
                ? "HERO_IDENTITY_UNVERIFIED"
                : hero.index() < 0 || hero.index() >= 256 * 64 ? "ENTITY_INDEX_OUT_OF_RANGE" : null;
        if (unavailable != null) {
            row.put("radiant_evidence", ReplayProbe.mapOf("status", unavailable));
            row.put("dire_evidence", ReplayProbe.mapOf("status", unavailable));
            return row;
        }
        // A dead primary entity can still have an authoritative bit. Do not infer HIDDEN
        // from death; life_state is context and never grants a living position.
        row.put("life_state", identity.property().apply("m_lifeState"));
        Reading rad = readTeam(radiant, 2, RADIANT_SOURCE, hero.index());
        Reading dir = readTeam(dire, 3, DIRE_SOURCE, hero.index());
        row.put("visible_to_radiant", rad.state());
        row.put("visible_to_dire", dir.state());
        row.put("radiant_evidence", rad.evidence());
        row.put("dire_evidence", dir.evidence());
        row.put("visibility_status", !UNKNOWN.equals(rad.state()) && !UNKNOWN.equals(dir.state())
                ? "OBSERVED" : UNKNOWN.equals(rad.state()) && UNKNOWN.equals(dir.state())
                ? UNKNOWN : "PARTIAL_UNKNOWN");
        return row;
    }

    private static Reading readTeam(HeroPositions.Source source, int team, String expectedClass, int entityIndex) {
        int wordIndex = entityIndex >>> 6;
        int bitIndex = entityIndex & 63;
        String path = FIELD + "." + Util.arrayIdxToString(wordIndex);
        Map<String, Object> evidence = ReplayProbe.mapOf(
                "status", "SOURCE_UNAVAILABLE",
                "source_entity_class", source == null ? null : source.entityClass(),
                "source_entity_handle", source == null ? null : source.handle(),
                "source_team", null,
                "property_path", path,
                "word_index", wordIndex,
                "bit_index", bitIndex,
                "raw_word_type", null,
                "raw_word_unsigned", null
        );
        if (source == null || !source.existent() || !source.active()) {
            return new Reading(UNKNOWN, evidence);
        }
        Object sourceTeam = source.property().apply("m_iTeamNum");
        evidence.put("source_team", sourceTeam);
        if (!expectedClass.equals(source.entityClass()) || !Integer.valueOf(team).equals(sourceTeam)) {
            evidence.put("status", "SOURCE_TEAM_UNVERIFIED");
            return new Reading(UNKNOWN, evidence);
        }
        Object word = source.property().apply(path);
        evidence.put("raw_word_type", word == null ? null : word.getClass().getName());
        if (!(word instanceof Long bits)) {
            evidence.put("status", word == null ? "WORD_UNAVAILABLE" : "UNSUPPORTED_WORD_TYPE");
            return new Reading(UNKNOWN, evidence);
        }
        // uint64[256], one bit per NPC entity INDEX; separate team entities supply
        // Radiant/Dire perspectives. No team-number bit guessing or geometry model.
        // Reference: gem-dota src/gem/extractors/visibility.py; verified on our .dem.
        evidence.put("status", "OBSERVED");
        evidence.put("raw_word_unsigned", Long.toUnsignedString(bits));
        return new Reading((bits & (1L << bitIndex)) != 0 ? "VISIBLE" : "HIDDEN", evidence);
    }
}
