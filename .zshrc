# Cache control - set to 0 to disable caching
ENABLE_CACHE=0
DEBUG=0

source ~/bin/commonrc

# ============================================================================
# OH-MY-ZSH SETUP (optional - gracefully degrades if not installed)
# ============================================================================
export ZSH="$HOME/.oh-my-zsh"
[[ "$DEBUG" == "1" ]] && echo "Starting oh-mh-zsh init, from $ZSH/oh-my-zsh.sh" >&2
if [[ -f "$ZSH/oh-my-zsh.sh" ]]; then
  [[ "$DEBUG" == "1" ]] && echo "Setting theme" >&2
  ZSH_THEME="intheloop"

  # Set ZSH_CUSTOM before loading plugins (defaults to $ZSH/custom)
  ZSH_CUSTOM="${ZSH_CUSTOM:-$ZSH/custom}"
  [[ "$DEBUG" == "1" ]] && echo "ZSH_CUSTOM set to: $ZSH_CUSTOM" >&2

  # Disable oh-my-zsh's compinit - we'll do it after to add caching
  skip_global_compinit=1

  # Uncomment the following line to enable command auto-correction.
  unsetopt correct
  ENABLE_CORRECTION="false"

  # Optional oh-my-zsh settings (uncomment to enable):
  # CASE_SENSITIVE="true"
  # HYPHEN_INSENSITIVE="true"
  # DISABLE_AUTO_UPDATE="true"
  # DISABLE_UPDATE_PROMPT="true"
  # UPDATE_ZSH_DAYS=13
  # DISABLE_MAGIC_FUNCTIONS="true"
  # DISABLE_LS_COLORS="true"
  # DISABLE_AUTO_TITLE="true"
  # COMPLETION_WAITING_DOTS="true"
  # DISABLE_UNTRACKED_FILES_DIRTY="true"
  # HIST_STAMPS="mm/dd/yyyy"
  # ZSH_CUSTOM=/path/to/new-custom-folder

  # Build plugins list with availability checks
  _plugins=(git)

  # Load datetime module for timing (if not already loaded)
  zmodload zsh/datetime 2>/dev/null

  # Function to load plugin if installed
  load_plugin_if_installed() {
    local plugin_name="$1"
    local start_time=$EPOCHREALTIME

    if [[ -d "$ZSH/plugins/$plugin_name" ]] || [[ -d "$ZSH_CUSTOM/plugins/$plugin_name" ]]; then
      _plugins+=($plugin_name)

      if [[ "$DEBUG" == "1" ]]; then
        local end_time=$EPOCHREALTIME
        local elapsed=$(( (end_time - start_time) * 1000 ))
        local elapsed_ms=${elapsed%.*}
        echo "Loaded plugin: $plugin_name (${elapsed_ms}ms)" >&2
      fi
      return 0
    else
      if [[ "$DEBUG" == "1" ]]; then
        local end_time=$EPOCHREALTIME
        local elapsed=$(( (end_time - start_time) * 1000 ))
        local elapsed_ms=${elapsed%.*}
        echo "Plugin $plugin_name not available (${elapsed_ms}ms)" >&2
      fi
    fi
    return 1
  }

  # Load pluginsc
  [[ "$DEBUG" == "1" ]] && echo "Loading plugins" >&2
  # Not working, replaced with manual script on commonrc
  #load_plugin_if_installed autoswitch_virtualenv
  setopt EXTENDED_GLOB
  ZSH_AUTOSUGGEST_STRATEGY=('history completion')
  ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE=200
  ZSH_AUTOSUGGEST_USE_ASYNC=1
  ZSH_AUTOSUGGEST_HISTORY_IGNORE="(cd|ls)(| *)"

  #git clone https://github.com/zsh-users/zsh-autosuggestions ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-autosuggestions
  load_plugin_if_installed zsh-autosuggestions
  # Disabled for performance - zsh-syntax-highlighting causes ~50-100ms slowdown
  # load_plugin_if_installed zsh-syntax-highlighting
  # load_plugin_if_installed you-should-use

  # Configure bat: disable pager, show only rule and header
  export BAT_PAGER=""
  export BAT_STYLE="rule,header"
  load_plugin_if_installed zsh-bat
  

  plugins=($_plugins)

  source $ZSH/oh-my-zsh.sh
fi

# Initialize completion system with optional caching
autoload -Uz compinit
if [[ "$ENABLE_CACHE" == "1" ]]; then
  # Caching enabled - only rebuild once per day
  if [[ -n ${HOME}/.zcompdump(#qNmh+24) ]]; then
    compinit
  else
    compinit -C
  fi
else
  # Caching disabled - always rebuild
  compinit
fi

# Completion styling
zstyle ':completion:*' menu yes select
zstyle ':completion:*:*:cd:*' menu yes select

# User configuration

# export MANPATH="/usr/local/man:$MANPATH"

# You may need to manually set your language environment
# export LANG=en_US.UTF-8

# Preferred editor for local and remote sessions
# if [[ -n $SSH_CONNECTION ]]; then
#   export EDITOR='vim'
# else
#   export EDITOR='mvim'
# fi

# Compilation flags
# export ARCHFLAGS="-arch x86_64"

# Set personal aliases, overriding those provided by oh-my-zsh libs,
# plugins, and themes. Aliases can be placed here, though oh-my-zsh
# users are encouraged to define aliases within the ZSH_CUSTOM folder.
# For a full list of active aliases, run `alias`.
#
# Example aliases
# alias zshconfig="mate ~/.zshrc"
# alias ohmyzsh="mate ~/.oh-my-zsh"

source ~/bin/commonrc-post
# bun completions
[ -s "$HOME/.bun/_bun" ] && source "$HOME/.bun/_bun"

# Inside Zellij, the terminal facing the remote is Zellij (xterm-256color), not
# Ghostty. Bypass Ghostty's ssh-terminfo wrapper and force a TERM the remote and
# Zellij both understand — otherwise remote apps emit xterm-ghostty sequences
# that Zellij can't render and the cursor jumps around.
if [[ -n "$ZELLIJ" ]]; then
  ssh() { TERM=xterm-256color command ssh "$@"; }
fi

# Machine-local overrides (untracked). Deliberate per-machine config goes here;
# installer PATH lines usually land in the ~/.zshrc stub instead.
[ -f ~/.zshrc.local ] && source ~/.zshrc.local
