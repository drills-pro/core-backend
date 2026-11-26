import { createSystem, defaultConfig, defineConfig, defineRecipe } from '@chakra-ui/react';

// Chakra's default outline border is gray.200, about 1.2:1 on white, so text boxes look borderless. Only the outline
// input and textarea get a darker gray; everything else stays Chakra's default.
const outlineBorder = defineRecipe({ variants: { variant: { outline: { borderColor: 'gray.500' } } } });

export const system = createSystem(
  defaultConfig,
  defineConfig({ theme: { recipes: { input: outlineBorder, textarea: outlineBorder } } }),
);
