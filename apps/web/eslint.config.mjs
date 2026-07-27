import next from "eslint-config-next";

/**
 * Flat ESLint config for Next.js 16. `eslint-config-next` ships a native flat
 * config array (core-web-vitals + TypeScript rules), so it is spread directly.
 * Generated shadcn/ui primitives are treated as protected files and excluded.
 */
const eslintConfig = [
  ...next,
  {
    ignores: [".next/**", "node_modules/**", "components/ui/**"],
  },
];

export default eslintConfig;
