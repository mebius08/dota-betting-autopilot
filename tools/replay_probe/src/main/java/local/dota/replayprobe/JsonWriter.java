package local.dota.replayprobe;

import java.lang.reflect.Array;
import java.util.Iterator;
import java.util.Map;

final class JsonWriter {
    private JsonWriter() {
    }

    static String toJson(Object value) {
        StringBuilder out = new StringBuilder(64 * 1024);
        append(out, value, 0);
        out.append('\n');
        return out.toString();
    }

    private static void append(StringBuilder out, Object value, int depth) {
        if (value == null) {
            out.append("null");
        } else if (value instanceof String || value instanceof Character || value instanceof Enum<?>) {
            quote(out, value.toString());
        } else if (value instanceof Boolean) {
            out.append(value);
        } else if (value instanceof Number number) {
            if ((number instanceof Double && !Double.isFinite(number.doubleValue()))
                    || (number instanceof Float && !Float.isFinite(number.floatValue()))) {
                out.append("null");
            } else {
                out.append(number);
            }
        } else if (value instanceof Map<?, ?> map) {
            appendMap(out, map, depth);
        } else if (value instanceof Iterable<?> iterable) {
            appendIterable(out, iterable, depth);
        } else if (value.getClass().isArray()) {
            appendArray(out, value, depth);
        } else {
            quote(out, value.toString());
        }
    }

    private static void appendMap(StringBuilder out, Map<?, ?> map, int depth) {
        out.append('{');
        if (!map.isEmpty()) {
            Iterator<? extends Map.Entry<?, ?>> iterator = map.entrySet().iterator();
            while (iterator.hasNext()) {
                Map.Entry<?, ?> entry = iterator.next();
                out.append('\n');
                indent(out, depth + 1);
                quote(out, String.valueOf(entry.getKey()));
                out.append(": ");
                append(out, entry.getValue(), depth + 1);
                if (iterator.hasNext()) {
                    out.append(',');
                }
            }
            out.append('\n');
            indent(out, depth);
        }
        out.append('}');
    }

    private static void appendIterable(StringBuilder out, Iterable<?> iterable, int depth) {
        out.append('[');
        Iterator<?> iterator = iterable.iterator();
        if (iterator.hasNext()) {
            while (iterator.hasNext()) {
                out.append('\n');
                indent(out, depth + 1);
                append(out, iterator.next(), depth + 1);
                if (iterator.hasNext()) {
                    out.append(',');
                }
            }
            out.append('\n');
            indent(out, depth);
        }
        out.append(']');
    }

    private static void appendArray(StringBuilder out, Object array, int depth) {
        out.append('[');
        int length = Array.getLength(array);
        for (int i = 0; i < length; i++) {
            out.append('\n');
            indent(out, depth + 1);
            append(out, Array.get(array, i), depth + 1);
            if (i + 1 < length) {
                out.append(',');
            }
        }
        if (length > 0) {
            out.append('\n');
            indent(out, depth);
        }
        out.append(']');
    }

    private static void indent(StringBuilder out, int depth) {
        out.append("  ".repeat(depth));
    }

    private static void quote(StringBuilder out, String value) {
        out.append('"');
        for (int i = 0; i < value.length(); i++) {
            char ch = value.charAt(i);
            switch (ch) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (ch < 0x20) {
                        out.append(String.format("\\u%04x", (int) ch));
                    } else {
                        out.append(ch);
                    }
                }
            }
        }
        out.append('"');
    }
}
