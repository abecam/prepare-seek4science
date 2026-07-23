# create_admin.rb
# Creates a Person + User pair on a SEEK instance and makes them admin.
#
# ⚠️ I can't verify the exact attribute/association names against your
# checked-out version of github.com/seek4science/seek from here, since
# these have shifted across SEEK releases. Before running this for real,
# open app/models/user.rb and app/models/person.rb in your copy of the
# repo (or run `bin/rails runner "puts Person.new.attribute_names"` /
# `puts User.new.attribute_names`) to confirm field names match what's
# below - the shape (User has credentials, Person has profile + roles)
# is stable, but a renamed field would make this raise, not silently
# corrupt anything.

person = Person.create!(
  first_name: "Admin",
  last_name:  "Denbi",
  email:      "admin@example.org"
)

user = User.create!(
  login:                 "denbi",
  email:                 "admin@example.org",
  password:              "denbi",
  password_confirmation: "denbi",
  person:                person
)
user.skip_confirmation! if user.respond_to?(:skip_confirmation!)  # bypass email confirmation
user.save!

# Making them a SEEK-wide admin is normally done via the roles system
# rather than a plain boolean column - check Person#is_admin= /
# app/models/role.rb in your version before trusting this line:
#person.is_admin = true if person.respond_to?(:is_admin=)
#person.save!

puts "Created user '#{user.login}' (person ##{person.id}), admin=#{person.respond_to?(:is_admin?) ? person.is_admin? : 'unknown'}"