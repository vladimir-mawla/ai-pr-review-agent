import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({
  baseDirectory: import.meta.dirname,
});

const eslintConfig = [
  // Flat config does not read .eslintignore -- without this, eslint also
  // lints Next's own generated .next/types/**/*.ts output and
  // next-env.d.ts, which are not this project's code.
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default eslintConfig;
