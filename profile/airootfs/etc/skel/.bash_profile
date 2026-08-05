[[ -f ~/.bashrc ]] && . ~/.bashrc

if [[ -z ${WAYLAND_DISPLAY:-} && ${XDG_VTNR:-0} == 1 ]]; then
  exec start-cosmic --in-login-shell
fi
