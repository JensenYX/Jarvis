export function boardGeometry(state) {
  return {
    rows: Number.isInteger(state?.R) && state.R >= 1 && state.R <= 6 ? state.R : 5,
    cols: Number.isInteger(state?.C) && state.C >= 1 && state.C <= 9 ? state.C : 9,
  };
}

export function supportsLawn(state, terrain) {
  return Boolean(
    state?.R === 5 &&
    state.C === 9 &&
    !state.CardKind &&
    state.DKind === 1 &&
    !state.HaveFog &&
    [1, 2, 3, 4, 5].every((row) => terrain?.[row] === 0 || terrain?.[row] === 1) &&
    [1, 2, 3, 4, 5].some((row) => terrain?.[row] === 1),
  );
}
