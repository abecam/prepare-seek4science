Set of scripts to:
* create a Seek instance adapted to SimpleVM ( https://simplevm.github.io/ ),
* create an initial user and some sample data
* migrate some actual data from Fairdom Hub ( https://fairdomhub.org/investigations/658 )
* and an experimental Ansible to create an initial user (probably useless due to the easiest Rails method used above)
  
To run it:
```
wget https://thebuoy.eu/sh/P3x/ (shortened)
mv P3x runMe.sh
chmod +x runMe.sh
./runMe.sh
```

It will not work fully, as the docker-compose needs to be adapted:

In the commented line: # RAILS_RELATIVE_URL_ROOT: /stillcondor_100 "/stillcondor_100" needs to be replaced by the instance sub-path. And the line needs to be uncommented.

Once adapted, Seek can be started again:

```
docker compose down
docker compose up -d
```

*If* the Seek instance is public, it is strongly recommended to change the admin password.
