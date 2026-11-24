#!/bin/bash
echo "Creating docker volumes for SEEK"
docker volume create --name=seek-filestore
docker volume create --name=seek-mysql-db
docker volume create --name=seek-solr-data
docker volume create --name=seek-cache

echo "Downloading docker-compose.yml and db.env"
#wget https://raw.githubusercontent.com/seek4science/seek/seek-1.18/docker-compose.yml
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/TestSeekDockerCompose/docker-compose.yml

# Set RAILS_RELATIVE_URL_ROOT (random subpath given by the SimpleVM setup) in the compose file
# Update the value
sudo /etc/simplevm/get_metadata.sh

LOCATION_URL="$(jq -r '.location_url // empty' /etc/simplevm/metadata.json 2>/dev/null)"
LOCATION_URL="${LOCATION_URL#/}"   # tolerate a leading slash
if [ -n "$LOCATION_URL" ]; then
  echo "Setting RAILS_RELATIVE_URL_ROOT to /${LOCATION_URL}"
  if grep -qE '^[[:space:]]*#?[[:space:]]*RAILS_RELATIVE_URL_ROOT:' docker-compose.yml; then
    # Line already present (commented or not): uncomment it and set the value
    sed -i -E "s|^([[:space:]]*)#?[[:space:]]*RAILS_RELATIVE_URL_ROOT:.*|\1RAILS_RELATIVE_URL_ROOT: /${LOCATION_URL}|" docker-compose.yml
  else
    # Line absent: add it to the environment of the "seek" service (not the workers)
    sed -i -E "/^  seek:/,/^  seek_workers:/ s|^([[:space:]]+)RAILS_ENV: production|&\n\1RAILS_RELATIVE_URL_ROOT: /${LOCATION_URL}|" docker-compose.yml
  fi
else
  echo "Warning: no location_url in /etc/simplevm/metadata.json, RAILS_RELATIVE_URL_ROOT not set" >&2
fi
mkdir docker
cd docker
wget https://raw.githubusercontent.com/seek4science/seek/seek-1.18/docker/db.env

mkdir solr
cd solr
mkdir seek
cd seek
mkdir conf
cd conf
wget https://raw.githubusercontent.com/seek4science/seek/refs/heads/seek-1.18/solr/seek/conf/solrconfig.xml

cd ..
echo "Starting SEEK containers"
docker compose up -d

echo "Waiting for SEEK to be ready"
# won't work as seek is not the name of the container
until docker logs seek | grep -q "Listening on"; do
  sleep 5
done
echo "SEEK is ready"

# Ansible needs to be installed first: sudo apt install ansible
echo "Creating initial admin user"
docker compose exec seek bundle exec rake db:seed:example_data

# Following is the tentative do create the initial user using ansible.
# It might be interesting to use ansible depending on how the webservices are set up, but for now, we will use the rake command above to create the initial user.
#echo "Getting localhost inventory file"
#wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/inventory.yml
#echo "Getting ansible playbook to create initial user"
#wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/CreateInitialuser.yml
#echo "Running ansible playbook to create initial user"
#ansible-playbook CreateInitialuser.yml
#ansible-playbook -i inventory.yml CreateInitialuser.yml \
#  --ask-vault-pass \
#  -e seek_admin_password='ChangeMe123!'

echo "Fetching and pushing data from investigation"
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/FetchPushFromInvestigation.py
python3 FetchPushFromInvestigation.py
