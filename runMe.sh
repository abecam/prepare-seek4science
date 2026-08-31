#!/bin/bash
echo "Creating docker volumes for SEEK"
docker volume create --name=seek-filestore
docker volume create --name=seek-mysql-db
docker volume create --name=seek-solr-data
docker volume create --name=seek-cache

echo "Downloading docker-compose.yml and db.env"
#wget https://raw.githubusercontent.com/seek4science/seek/seek-1.18/docker-compose.yml
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/TestSeekDockerCompose/docker-compose.yml
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
echo "Getting localhost inventory file"
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/inventory.yml
echo "Getting ansible playbook to create initial user"
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/CreateInitialuser.yml
echo "Running ansible playbook to create initial user"
#ansible-playbook CreateInitialuser.yml
ansible-playbook -i inventory.yml CreateInitialuser.yml \
  --ask-vault-pass \
  -e seek_admin_password='ChangeMe123!'

echo "Fetching and pushing data from investigation"
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/FetchPushFromInvestigation.py
wget https://raw.githubusercontent.com/abecam/prepare-seek4science/main/config.json
python3 FetchPushFromInvestigation.py