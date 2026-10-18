package local.dota.replayprobe;

import skadistats.clarity.model.Entity;
import skadistats.clarity.model.FieldPath;

import java.util.Map;
import java.util.function.Function;

/** Current selected-hero observations only; no entity search or position cache. */
final class HeroPositions {
    static final String CELL_X = "CBodyComponent.m_cellX";
    static final String CELL_Y = "CBodyComponent.m_cellY";
    static final String OFFSET_X = "CBodyComponent.m_vecX";
    static final String OFFSET_Y = "CBodyComponent.m_vecY";
    static final double CELL_SIZE = 128.0;

    private HeroPositions() {}

    record Source(int handle, String entityClass, boolean existent, boolean active,
                  Function<String, Object> property) {}

    static Map<String, Object> observe(int slot, int team, Integer heroId, Integer selectedHandle, Entity hero) {
        Source source = hero == null ? null : new Source(
                hero.getHandle(), hero.getDtClass().getDtName(), hero.isExistent(), hero.isActive(),
                name -> readProperty(hero, name)
        );
        return observeSource(slot, team, heroId, selectedHandle, source);
    }

    static Map<String, Object> observeSource(int slot, int team, Integer heroId, Integer selectedHandle,
                                            Source source) {
        Map<String, Object> row = ReplayProbe.mapOf(
                "player_slot", slot,
                "team", team == 2 ? "RADIANT" : "DIRE",
                "hero_id", heroId,
                "selected_hero_handle", selectedHandle,
                "hero_entity_handle", source == null ? null : source.handle(),
                "hero_entity_class", source == null ? null : source.entityClass(),
                "life_state", null,
                "position_status", "ENTITY_UNAVAILABLE",
                "x", null, "y", null,
                "cell_x", null, "cell_y", null, "offset_x", null, "offset_y", null
        );
        if (source == null) {
            return row;
        }
        if (!source.existent() || !source.active()) {
            row.put("position_status", "ENTITY_INACTIVE");
            return row;
        }
        if (selectedHandle == null || selectedHandle != source.handle()
                || heroId == null || heroId <= 0 || (team != 2 && team != 3)
                || !source.entityClass().startsWith("CDOTA_Unit_Hero_")
                || !Integer.valueOf(team).equals(source.property().apply("m_iTeamNum"))
                || Boolean.TRUE.equals(source.property().apply("m_bIsClone"))) {
            row.put("position_status", "IDENTITY_UNVERIFIED");
            return row;
        }

        Object lifeState = source.property().apply("m_lifeState");
        row.put("life_state", lifeState);
        if (!(lifeState instanceof Number state)) {
            row.put("position_status", "LIFE_STATE_UNAVAILABLE");
            return row;
        }
        if (state.doubleValue() != 0) {
            // A corpse entity may keep its old position or move off-map. It is not a living hero observation.
            row.put("position_status", "HERO_NOT_ALIVE");
            return row;
        }

        Object cellX = source.property().apply(CELL_X);
        Object cellY = source.property().apply(CELL_Y);
        Object offsetX = source.property().apply(OFFSET_X);
        Object offsetY = source.property().apply(OFFSET_Y);
        Double x = coordinate(cellX, offsetX);
        Double y = coordinate(cellY, offsetY);
        if (x == null || y == null) {
            row.put("position_status", "COORDINATES_UNAVAILABLE");
            return row;
        }
        row.put("position_status", "OBSERVED");
        row.put("x", x);
        row.put("y", y);
        row.put("cell_x", cellX);
        row.put("cell_y", cellY);
        // Match the arithmetic precision in x/y when serializing decoded Float components.
        row.put("offset_x", ((Number) offsetX).doubleValue());
        row.put("offset_y", ((Number) offsetY).doubleValue());
        return row;
    }

    static Double coordinate(Object cellValue, Object offsetValue) {
        if (!(cellValue instanceof Number cell) || !(offsetValue instanceof Number offset)) {
            return null;
        }
        double c = cell.doubleValue();
        double v = offset.doubleValue();
        // Dota Source 2: uint16 cell and a quantized offset in [0, 256), NOT [0, 128).
        if (!Double.isFinite(c) || c < 0 || c > 65535 || c != Math.rint(c)
                || !Double.isFinite(v) || v < 0 || v >= 256) {
            return null;
        }
        // Clarity's Source 2 position recipe: https://github.com/skadistats/clarity/issues/211
        // Retain the native cell-grid origin; do not guess a map/image translation.
        return c * CELL_SIZE + v;
    }

    private static Object readProperty(Entity hero, String name) {
        FieldPath path = hero.getDtClass().getFieldPathForName(name);
        if (path == null) {
            return null;
        }
        try {
            return hero.getPropertyForFieldPath(path);
        } catch (RuntimeException ignored) {
            return null;
        }
    }
}
