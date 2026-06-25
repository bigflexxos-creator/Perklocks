/**
 * MarkdownText — Lightweight inline-markdown renderer.
 *
 * Built specifically for AI-generated "Why this pick" explanations from
 * Claude / GPT, which routinely emit:
 *   - `## Section Heading`
 *   - `**bold inline**`
 *   - `*italic inline*`
 *   - `- bullet list item`
 *   - `> blockquote`
 *
 * Before this component, the detail screen rendered raw markdown as
 * plain text — users saw literal `## Why This Pick?` and `**Bold**`
 * characters and (rightly) thought the screen was broken.
 *
 * Intentionally minimal — no nested lists, no tables, no link parsing.
 * Pure JS, no native deps, works on web + iOS + Android Expo.
 */
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { COLORS } from "@/src/theme";

type Props = {
  children: string;
  /** Base text style applied to every paragraph. */
  style?: any;
};

/**
 * Parse a single line's inline markdown (`**bold**`, `*italic*`,
 * `` `mono` ``) into an array of <Text> spans. Plain text passes
 * through unchanged.
 */
function renderInline(line: string, keyPrefix: string): React.ReactNode[] {
  // Tokenizer covers **bold**, *italic*, `mono` — in order of precedence.
  // Bold must be matched BEFORE italic so `**foo**` doesn't get treated
  // as italic-italic.
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
  const parts = line.split(pattern).filter((p) => p !== "");
  return parts.map((part, idx) => {
    const key = `${keyPrefix}-${idx}`;
    if (part.startsWith("**") && part.endsWith("**")) {
      return <Text key={key} style={styles.bold}>{part.slice(2, -2)}</Text>;
    }
    if (part.startsWith("*") && part.endsWith("*")) {
      return <Text key={key} style={styles.italic}>{part.slice(1, -1)}</Text>;
    }
    if (part.startsWith("`") && part.endsWith("`")) {
      return <Text key={key} style={styles.mono}>{part.slice(1, -1)}</Text>;
    }
    return <Text key={key}>{part}</Text>;
  });
}

export function MarkdownText({ children, style }: Props) {
  if (!children) return null;

  // Split into logical "lines" — but keep blank lines so we render
  // proper paragraph spacing instead of one wall of text.
  const lines = children.replace(/\r\n/g, "\n").split("\n");

  // Group consecutive bullet lines into a single list block so the
  // spacing looks tight (matches the rest of the app's design).
  const blocks: React.ReactNode[] = [];
  let bulletGroup: string[] = [];

  const flushBullets = (key: string) => {
    if (bulletGroup.length === 0) return;
    blocks.push(
      <View key={key} style={styles.bulletGroup}>
        {bulletGroup.map((b, i) => (
          <View key={`b-${i}`} style={styles.bulletRow}>
            <Text style={styles.bulletDot}>•</Text>
            <Text style={[styles.body, style, styles.bulletText]}>
              {renderInline(b, `${key}-${i}`)}
            </Text>
          </View>
        ))}
      </View>,
    );
    bulletGroup = [];
  };

  lines.forEach((rawLine, idx) => {
    const line = rawLine.trim();
    const key = `l-${idx}`;

    // Blank line — flush bullets and add paragraph break.
    if (line === "") {
      flushBullets(`bg-${idx}`);
      return;
    }

    // ## or ### heading
    const headingMatch = line.match(/^(#{1,3})\s+(.*)$/);
    if (headingMatch) {
      flushBullets(`bg-${idx}`);
      const level = headingMatch[1].length;
      const text = headingMatch[2];
      blocks.push(
        <Text
          key={key}
          style={[
            styles.heading,
            level === 1 && styles.h1,
            level === 2 && styles.h2,
            level === 3 && styles.h3,
          ]}
        >
          {renderInline(text, key)}
        </Text>,
      );
      return;
    }

    // - or * bullet
    const bulletMatch = line.match(/^[-*]\s+(.*)$/);
    if (bulletMatch) {
      bulletGroup.push(bulletMatch[1]);
      return;
    }

    // > blockquote
    if (line.startsWith("> ")) {
      flushBullets(`bg-${idx}`);
      blocks.push(
        <View key={key} style={styles.quote}>
          <Text style={[styles.body, style, styles.quoteText]}>
            {renderInline(line.slice(2), key)}
          </Text>
        </View>,
      );
      return;
    }

    // Plain paragraph
    flushBullets(`bg-${idx}`);
    blocks.push(
      <Text key={key} style={[styles.body, style]}>
        {renderInline(line, key)}
      </Text>,
    );
  });

  flushBullets("bg-end");

  return <View>{blocks}</View>;
}

const styles = StyleSheet.create({
  body: {
    color: COLORS.textSecondary,
    fontSize: 13,
    lineHeight: 19,
    marginTop: 8,
  },
  bold: {
    color: COLORS.textPrimary,
    fontWeight: "800",
  },
  italic: {
    fontStyle: "italic",
  },
  mono: {
    fontFamily: "Courier",
    color: COLORS.voltBlue,
    fontSize: 12,
  },
  heading: {
    color: COLORS.textPrimary,
    fontWeight: "900",
    letterSpacing: -0.2,
    marginTop: 14,
    marginBottom: 2,
  },
  h1: { fontSize: 16 },
  h2: { fontSize: 14, letterSpacing: 0.4, textTransform: "uppercase" },
  h3: { fontSize: 13, letterSpacing: 0.6, textTransform: "uppercase", color: COLORS.textMuted },
  bulletGroup: {
    marginTop: 8,
    gap: 4,
  },
  bulletRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 8,
    paddingLeft: 2,
  },
  bulletDot: {
    color: COLORS.voltBlue,
    fontSize: 14,
    lineHeight: 19,
    fontWeight: "900",
  },
  bulletText: {
    flex: 1,
    marginTop: 0,
  },
  quote: {
    borderLeftWidth: 3,
    borderLeftColor: COLORS.voltBlue + "66",
    paddingLeft: 10,
    marginTop: 10,
  },
  quoteText: {
    fontStyle: "italic",
    color: COLORS.textMuted,
    marginTop: 0,
  },
});
