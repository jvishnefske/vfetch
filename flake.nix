{
  description = "xfetch - yt-dlp powered web video player (FastAPI + hls.js)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});

      # Source tree minus build/venv cruft so the store path is reproducible.
      mkSrc = pkgs: pkgs.lib.cleanSourceWith {
        src = ./.;
        filter = path: type:
          let b = baseNameOf path; in
          b != ".venv" && b != "__pycache__" && b != "result"
          && !(pkgs.lib.hasSuffix ".pyc" path);
      };

      mkPackage = pkgs:
        let
          pythonEnv = pkgs.python3.withPackages (ps: with ps; [
            fastapi uvicorn yt-dlp httpx jinja2
          ]);
          # yt-dlp needs a server-side JS runtime for signature challenges.
          # Deno is yt-dlp's preferred engine; bundle it so PATH is guaranteed.
          jsRuntime = pkgs.deno;
          src = mkSrc pkgs;
        in
        pkgs.writeShellApplication {
          name = "xfetch";
          runtimeInputs = [ pythonEnv jsRuntime ];
          text = ''
            export PYTHONPATH=${src}''${PYTHONPATH:+:$PYTHONPATH}
            exec uvicorn app.main:app \
              --host "''${XFETCH_HOST:-0.0.0.0}" \
              --port "''${XFETCH_PORT:-8000}" \
              --proxy-headers "$@"
          '';
        };
    in
    {
      packages = forAllSystems (system: pkgs: {
        default = mkPackage pkgs;
        xfetch = mkPackage pkgs;
      });

      apps = forAllSystems (system: pkgs: {
        default = {
          type = "app";
          program = "${mkPackage pkgs}/bin/xfetch";
        };
      });

      devShells = forAllSystems (system: pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [
              fastapi uvicorn yt-dlp httpx jinja2
            ]))
            pkgs.deno
          ];
          shellHook = ''
            echo "xfetch dev shell — run: uvicorn app.main:app --reload --port 8000"
          '';
        };
      });

      # NixOS module: `services.xfetch.enable = true;` gives you a systemd
      # service running the packaged app under a locked-down DynamicUser.
      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.xfetch;
        in
        {
          options.services.xfetch = {
            enable = lib.mkEnableOption "xfetch yt-dlp web video player";
            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.system}.default;
              description = "The xfetch package to run.";
            };
            host = lib.mkOption {
              type = lib.types.str;
              default = "0.0.0.0";
              description = "Address uvicorn binds to (all interfaces by default).";
            };
            port = lib.mkOption {
              type = lib.types.port;
              default = 8000;
              description = "Port uvicorn listens on.";
            };
            openFirewall = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Open the listen port in the firewall.";
            };
          };

          config = lib.mkIf cfg.enable {
            systemd.services.xfetch = {
              description = "xfetch - yt-dlp powered web video player";
              wantedBy = [ "multi-user.target" ];
              after = [ "network-online.target" ];
              wants = [ "network-online.target" ];
              environment = {
                XFETCH_HOST = cfg.host;
                XFETCH_PORT = toString cfg.port;
                # SQLite recommender DB lives in the persistent StateDirectory.
                XFETCH_DB = "/var/lib/xfetch/xfetch.db";
                # ProtectSystem=strict makes everything read-only except the
                # managed dirs, so point caches at CacheDirectory.
                HOME = "/var/cache/xfetch";
                DENO_DIR = "/var/cache/xfetch/deno";
                XDG_CACHE_HOME = "/var/cache/xfetch";
              };
              serviceConfig = {
                ExecStart = "${cfg.package}/bin/xfetch";
                Restart = "on-failure";
                RestartSec = 3;
                # Sandboxing
                DynamicUser = true;
                RuntimeDirectory = "xfetch";
                StateDirectory = "xfetch";
                CacheDirectory = "xfetch";
                NoNewPrivileges = true;
                PrivateTmp = true;
                PrivateDevices = true;
                ProtectSystem = "strict";
                ProtectHome = true;
                ProtectKernelTunables = true;
                ProtectKernelModules = true;
                ProtectControlGroups = true;
                RestrictNamespaces = true;
                RestrictRealtime = true;
                RestrictSUIDSGID = true;
                LockPersonality = true;
                RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
                SystemCallFilter = [ "@system-service" ];
                SystemCallErrorNumber = "EPERM";
                CapabilityBoundingSet = "";
              };
            };

            networking.firewall.allowedTCPPorts =
              lib.mkIf cfg.openFirewall [ cfg.port ];
          };
        };
    };
}
