from django.contrib.auth.decorators import login_required
from django.shortcuts import render_to_response, get_object_or_404
from django.http import HttpResponseRedirect
from django.template.context import RequestContext
from django.core.urlresolvers import reverse
from django.views.generic.list_detail import object_list, object_detail
from django.db.models import Count
from django.forms.models import modelformset_factory
from django.db.models import Max, Sum
from django.contrib.auth.models import Group
from django.views.decorators.cache import cache_control
from django.http import HttpResponse
from django.template import loader, Context
import datetime
import codecs

from praktomat.tasks.models import Task
from praktomat.solutions.models import Solution, SolutionFile
from praktomat.solutions.forms import SolutionFormSet
from praktomat.attestation.models import Attestation, AnnotatedSolutionFile, RatingResult, Script, RatingScaleItem
from praktomat.attestation.forms import AnnotatedFileFormSet, RatingResultFormSet, AttestationForm, AttestationPreviewForm, ScriptForm
from praktomat.accounts.templatetags.in_group import in_group
from praktomat.accounts.models import User
from praktomat.accounts.views import access_denied


@login_required
def statistics(request,task_id):
	task = get_object_or_404(Task, pk=task_id)
	
	if not (in_group(request.user,'Trainer') or request.user.is_superuser):
		return access_denied(request)
	
	final_solution_ids = map(lambda x:x['id__max'], task.solution_set.values('author').annotate(Max('id')))
	final_solutions = task.solution_set.filter(id__in=final_solution_ids)
	unfinal_solutions = task.solution_set.exclude(id__in=final_solution_ids)
	final_solution_count = final_solutions.count()
	user_count = Group.objects.get(name='User').user_set.filter(is_active=True).count()
	
	submissions = []
	submissions_final = []
	acc_submissions = [0]
	creation_dates = map(lambda dict:dict['creation_date'].date(), unfinal_solutions.values('creation_date'))
	creation_dates_final = map(lambda dict:dict['creation_date'].date(), final_solutions.values('creation_date'))
	for date in daterange(task.publication_date.date(), min(task.submission_date.date(), datetime.date.today())):
		submissions.append(creation_dates.count(date))
		submissions_final.append(creation_dates_final.count(date))
		acc_submissions.append(acc_submissions[-1]+submissions_final[-1])
	acc_submissions.pop(0)
	acc_submissions = map(lambda submissions: float(submissions)/user_count, acc_submissions)
	
	creation_times = map(lambda dict:[(dict['creation_date'].time().hour*3600+dict['creation_date'].time().minute*60)*1000, dict['creation_date'].weekday()], unfinal_solutions.values('creation_date'))
	creation_times_final = map(lambda dict:[(dict['creation_date'].time().hour*3600+dict['creation_date'].time().minute*60)*1000, dict['creation_date'].weekday()], final_solutions.values('creation_date'))
	
	attestations = Attestation.objects.filter(solution__task__id=task.id).filter(final=True).filter(published=False).aggregate(final=Count('id'))
	attestations.update( Attestation.objects.filter(solution__task__id=task.id).filter(published=True).aggregate(published=Count('id')) )
	attestations['all'] = final_solution_count
	
	final_grade_rating_scale_items = "['" + "','".join(task.final_grade_rating_scale.ratingscaleitem_set.values_list('name', flat=True)) + "']"
	ratings = RatingScaleItem.objects.filter(attestation__solution__task=task_id).annotate(Count('id')).values_list('position','id__count')
	ratings = map(lambda x:[x[0]-1,x[1]], ratings) # [(1, 2), (2, 2), (3, 2), (4, 2)] => [[0, 2], [1, 2], [2, 2], [3, 2]]

	return render_to_response("attestation/statistics.html", {'task':task, \
															'user_count': user_count, 'solution_count': final_solution_count,\
															'submissions':submissions, 'submissions_final':submissions_final, 'creation_times':creation_times, 'creation_times_final':creation_times_final, 'acc_submissions':acc_submissions, \
															'attestations':attestations, \
															'final_grade_rating_scale_items' :final_grade_rating_scale_items, 'ratings':ratings\
															}, context_instance=RequestContext(request))

def daterange(start_date, end_date):
    for n in range((end_date - start_date).days + 1):
        yield start_date + datetime.timedelta(n)
	
@login_required
@cache_control(must_revalidate=True, no_cache=True, no_store=True, max_age=0) #reload the page from the server even if the user used the back button
def attestation_list(request, task_id):
	if not (in_group(request.user,'Tutor,Trainer') or request.user.is_superuser):
		return access_denied(request)
	
	task = Task.objects.get(pk=task_id)
	requestuser = request.user
	
	solutions = Solution.objects.filter(task__id=task_id).all()
	if in_group(requestuser,'Tutor'): # only the trainer / admin can see it all
		solutions = solutions.filter(author__tutorial__tutors__pk=request.user.id)
	# simulate max id group by author to get the newest solution of each author
	solution_ids = map(lambda x:x['id__max'], solutions.values('author').annotate(Max('id')))
	solutions = Solution.objects.filter(id__in=solution_ids)
	# don't allow a new attestation if one already exists
	solution_list = map(lambda solution:(solution,not in_group(requestuser,'Trainer') and not solution.attestations_by(requestuser)), solutions)
	
	# first published => all published
	try:
		published = solutions[0].attestations_by(requestuser)[0].published
	except IndexError:
		published = False
	
	all_solutions_attested = not reduce(lambda x,y: x or y, [new_attest_possible for (solution, new_attest_possible) in solution_list], False)
	all_attestations_final = reduce(lambda x,y: x and y, 
								map(lambda attestation: attestation.final , 
									sum([list(solution.attestations_by(requestuser)) for solution in solutions],[])), True)
	publishable = all_solutions_attested and all_attestations_final and task.expired()
	
	if request.method == "POST" and publishable:
		for solution in solutions:
			for attestation in solution.attestations_by(requestuser):
				attestation.publish()
				published = True
	data = {'task':task, 'requestuser':requestuser,'solution_list': solution_list, 'published': published, 'publishable': publishable}
	return render_to_response("attestation/attestation_list.html", data, context_instance=RequestContext(request))
	
@login_required
def new_attestation(request, solution_id):
	if not (in_group(request.user,'Tutor,Trainer') or request.user.is_superuser):
		return access_denied(request)
	
	solution = get_object_or_404(Solution, pk=solution_id)
	# If there already is an attestation by this user redirect to edit page
	attestations_of_request_user = solution.attestation_set.filter(author=request.user)
	if attestations_of_request_user.count() > 0:
		return HttpResponseRedirect(reverse('edit_attestation', args=[attestations_of_request_user[0].id]))
		
	attest = Attestation(solution = solution, author = request.user)
	attest.save()
	for solutionFile in  solution.solutionfile_set.all():
		annotatedFile = AnnotatedSolutionFile(attestation = attest, solution_file=solutionFile, content=solutionFile.content())
		annotatedFile.save()
	for rating in solution.task.rating_set.all():
		ratingResult = RatingResult(attestation = attest, aspect=rating.aspect)
		ratingResult.save()
	return HttpResponseRedirect(reverse('edit_attestation', args=[attest.id]))
	
		
@login_required	
def edit_attestation(request, attestation_id):
	if not (in_group(request.user,'Tutor,Trainer') or request.user.is_superuser):
		return access_denied(request)
	
	attest = get_object_or_404(Attestation, pk=attestation_id)
	if attest.published or attest.author != request.user:
		# If if this attestation is allready final or not by this user redirect to view_attestation
		return HttpResponseRedirect(reverse('view_attestation', args=[attestation_id]))
	
	solution = attest.solution
	model_solution = solution.task.model_solution

	if request.method == "POST":
		attestForm = AttestationForm(request.POST, instance=attest, prefix='attest')
		attestFileFormSet = AnnotatedFileFormSet(request.POST, instance=attest, prefix='attestfiles')
		ratingResultFormSet = RatingResultFormSet(request.POST, instance=attest, prefix='ratingresult')
		if attestForm.is_valid() and attestFileFormSet.is_valid() and ratingResultFormSet.is_valid():
			attestForm.save()
			attest.final = False
			attest.save()
			attestFileFormSet.save()
			ratingResultFormSet.save()
			return HttpResponseRedirect(reverse('view_attestation', args=[attestation_id]))
	else:
		attestForm = AttestationForm(instance=attest, prefix='attest')
		attestFileFormSet = AnnotatedFileFormSet(instance=attest, prefix='attestfiles')
		ratingResultFormSet = RatingResultFormSet(instance=attest, prefix='ratingresult')
	return render_to_response("attestation/attestation_edit.html", {"attestForm": attestForm, "attestFileFormSet": attestFileFormSet, "ratingResultFormSet":ratingResultFormSet, "solution": solution, "model_solution":model_solution},	context_instance=RequestContext(request))

@login_required	
def view_attestation(request, attestation_id):
	attest = get_object_or_404(Attestation, pk=attestation_id)
	if not (attest.solution.author == request.user or in_group(request.user,'Tutor,Trainer') or request.user.is_superuser):
		return access_denied(request)

	if request.method == "POST":
		form = AttestationPreviewForm(request.POST, instance=attest)
		if form.is_valid():
			form.save()
			return HttpResponseRedirect(reverse('attestation_list', args=[attest.solution.task.id]))
	else:
		form = AttestationPreviewForm(instance=attest)
		submitable = attest.author == request.user and not attest.published
		return render_to_response("attestation/attestation_view.html", {"attest": attest, 'submitable':submitable, 'form':form},	context_instance=RequestContext(request))

@login_required	
def rating_overview(request):
	if not (in_group(request.user,'Trainer') or request.user.is_superuser):
		return access_denied(request)
	
	attestations = Attestation.objects.filter(published=True)
	
	attestation_dict = {} 	#{(task_id,user_id):attestation}
	for attestation in attestations:
			attestation_dict[attestation.solution.task_id, attestation.solution.author_id] = attestation
	
	task_id_list = Task.objects.filter(submission_date__lt = datetime.datetime.now).order_by('publication_date','submission_date').values_list('id', flat=True)
	user_id_list = User.objects.filter(groups__name='User').filter(is_active=True).order_by('last_name','first_name').values_list('id', flat=True)
	
	task_list = map(lambda task_id:Task.objects.get(id=task_id), task_id_list)	
	
	rating_list = []
	for user_id in user_id_list:
		rating_for_user_list = [User.objects.get(id=user_id)]
		for task_id in task_id_list:
			try:
				rating = attestation_dict[task_id,user_id]
			except KeyError:
				rating = None
			rating_for_user_list.append(rating)
		rating_list.append(rating_for_user_list)
		
	FinalGradeFormSet = modelformset_factory(User, fields=('final_grade',), extra=0)
	# corresponding user to user_id_list in reverse order! important for easy displaying in template
	user = User.objects.filter(groups__name='User').filter(is_active=True).order_by('-last_name','-first_name')
	
	script = Script.objects.get_or_create(id=1)[0]
	
	if request.method == "POST":
		final_grade_formset = FinalGradeFormSet(request.POST, request.FILES, queryset = user)
		script_form = ScriptForm(request.POST, instance=script)
		if final_grade_formset.is_valid() and script_form.is_valid():
			final_grade_formset.save()
			script_form.save()
	else:
		final_grade_formset = FinalGradeFormSet(queryset = user)
		script_form = ScriptForm(instance=script)
	
	return render_to_response("attestation/rating_overview.html", {'rating_list':rating_list, 'task_list':task_list, 'final_grade_formset':final_grade_formset, 'script_form':script_form},	context_instance=RequestContext(request))

@login_required	
def rating_export(request):
	if not (in_group(request.user,'Trainer') or request.user.is_superuser):
		return access_denied(request)
	
	attestations = Attestation.objects.filter(published=True)
	
	attestation_dict = {} 	#{(task_id,user_id):rating}
	for attestation in attestations:
			attestation_dict[attestation.solution.task_id, attestation.solution.author_id] = attestation
	
	task_id_list = Task.objects.filter(submission_date__lt = datetime.datetime.now).order_by('publication_date','submission_date').values_list('id', flat=True)
	user_id_list = User.objects.filter(groups__name='User').filter(is_active=True).order_by('last_name','first_name').values_list('id', flat=True)
	
	task_list = map(lambda task_id:Task.objects.get(id=task_id), task_id_list)	
	
	rating_list = []
	for user_id in user_id_list:
		rating_for_user_list = [User.objects.get(id=user_id)]
		for task_id in task_id_list:
			try:
				rating = attestation_dict[task_id,user_id]
			except KeyError:
				rating = None
			rating_for_user_list.append(rating)
		rating_list.append(rating_for_user_list)
	
	response = HttpResponse(mimetype='text/csv')
	response['Content-Disposition'] = 'attachment; rating_export.csv'

	t = loader.get_template('attestation/rating_export.csv')
	c = Context({'rating_list':rating_list, 'task_list':task_list})
	#response.write(u'\ufeff') setting utf-8 BOM for Exel doesn't work
	response.write(t.render(c))
	return response
	
