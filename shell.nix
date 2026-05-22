{ pkgs ? import <nixpkgs> {} }:

let
  pgData = "/tmp/postgres-dev";
  pgPort = "5432";
  pgPassword = "devpassword";
in
pkgs.mkShell {
  nativeBuildInputs = with pkgs.buildPackages; [
    uv statix basedpyright ollama cargo wayclip awscli2 jq
    pkgs.postgresql pkgs.openssl
  ];

  shellHook = ''
    # Source .env if it exists
    if [ -f .env ]; then
      set -a
      source .env
      set +a
    fi

    if [ "''${ENVIRONMENT,,}" != "prod" ]; then
      export PGDATA="${pgData}"
      export PGPORT="${pgPort}"
      export PGHOST="/tmp"
      export PGUSER="postgres"
      export PGPASSWORD="${pgPassword}"

      if [ ! -d "$PGDATA" ]; then
        echo "Initializing PostgreSQL data directory..."
        initdb --auth=md5 --username=postgres --pwfile=<(echo "${pgPassword}") "$PGDATA"

        ${pkgs.openssl}/bin/openssl req -new -x509 -days 365 -nodes \
          -out "$PGDATA/server.crt" \
          -keyout "$PGDATA/server.key" \
          -subj "/CN=localhost" 2>/dev/null
        chmod 600 "$PGDATA/server.key"

        echo "ssl = on"                          >> "$PGDATA/postgresql.conf"
        echo "ssl_cert_file = 'server.crt'"     >> "$PGDATA/postgresql.conf"
        echo "ssl_key_file  = 'server.key'"     >> "$PGDATA/postgresql.conf"
        echo "unix_socket_directories = '/tmp'" >> "$PGDATA/postgresql.conf"

        cat > "$PGDATA/pg_hba.conf" <<EOF
local   all all               md5
host    all all 127.0.0.1/32  md5
host    all all ::1/128        md5
hostssl all all 127.0.0.1/32  md5
hostssl all all ::1/128        md5
EOF
      fi

      if ! pg_ctl status -D "$PGDATA" > /dev/null 2>&1; then
        echo "Starting PostgreSQL..."
        pg_ctl start -D "$PGDATA" \
          -l "$PGDATA/postgres.log" \
          -o "-p ${pgPort} -k /tmp"
        echo "PostgreSQL started on port ${pgPort} (logs: $PGDATA/postgres.log)"
      else
        echo "PostgreSQL already running."
      fi


      trap 'echo "Stopping PostgreSQL..."; pg_ctl stop -D "$PGDATA" -m fast' EXIT
    else
      echo "Production environment detected — skipping local PostgreSQL."
    fi
  '';
}
