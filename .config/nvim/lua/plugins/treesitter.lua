-- Tree-sitter incremental selection: Ctrl+W expands by syntax node (IntelliJ-style),
-- Ctrl+Shift+W shrinks. Same idea as editor::SelectLargerSyntaxNode in Zed.
return {
  "nvim-treesitter/nvim-treesitter",
  opts = {
    incremental_selection = {
      enable = true,
      keymaps = {
        init_selection = "<C-w>",
        node_incremental = "<C-w>",
        node_decremental = "<C-S-w>",
        scope_incremental = false,
      },
    },
  },
}
